"""Core automation layer: browser automation, API calls, application workflow.

Refactored from legacy bot.py. Public class: WoonnetClient (alias WoonnetBot for
backwards compatibility with older UI / scripts).
"""

from __future__ import annotations

import os, sys, time, re, json, threading, logging, requests
from datetime import datetime, timezone
from queue import Queue
from typing import List, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException
from urllib.parse import urljoin

from reporting import send_discord_report
from config import (
    BASE_URL, LOGIN_URL, DISCOVERY_URL, API_DISCOVERY_URL, API_DETAILS_SINGLE_URL,
    API_TIMER_URL, USERNAME_FIELD_SELECTOR, PASSWORD_FIELD_SELECTOR,
    LOGIN_BUTTON_SELECTOR, LOGOUT_LINK_SELECTOR, USER_AGENT, APPLICATION_HOUR
)

if not getattr(sys, 'frozen', False):  # only import manager when not frozen
    from webdriver_manager.chrome import ChromeDriverManager  # type: ignore


class WoonnetClient:
    """Automates interaction with WoonnetRijnmond website (login, discover, apply).

    Parameters:
        status_queue: Queue for pushing user-friendly status messages.
        logger: Python logger instance.
        log_file_path: Path to active log file for inclusion in reports.
        enable_browser: When False, skip all Selenium / driver initialization (useful for
            unit tests where only parsing / HTTP logic is exercised).
            Can also be forced off by environment variable WOONNET_NO_BROWSER=1.
    """
    def __init__(self, status_queue: Queue, logger: logging.Logger, log_file_path: str, *, enable_browser: bool = True):
        self.driver = None  # webdriver.Chrome | None
        self.status_queue = status_queue
        self.logger = logger
        self.log_file_path = log_file_path
        self.stop_event = threading.Event()
        self.session = requests.Session()
        self.is_logged_in = False
        # Instrumentation
        self.debug = False
        self.trace_http_bodies = True
        self._last_listing_ids = set()
        self._empty_runs = 0
        self.cache_path = None
        self._orig_request_func = None
        # Respect env override
        if os.environ.get("WOONNET_NO_BROWSER") == "1":
            enable_browser = False
        # Browser service (optional)
        self.service = None
        if enable_browser:
            try:
                if getattr(sys, 'frozen', False):
                    driver_path = os.path.join(sys._MEIPASS, "chromedriver.exe")  # type: ignore
                    self.service = ChromeService(executable_path=driver_path)
                    self._log(f"Bundle mode: Using chromedriver from {driver_path}")
                else:
                    self._log("Script mode: Installing/updating chromedriver with webdriver-manager...")
                    self.service = ChromeService(ChromeDriverManager().install())  # type: ignore
            except Exception as e:
                self._log(f"CRITICAL: Failed to initialize ChromeService: {e}", "error")
                self._report_error(e, "during ChromeService initialization")
                self.service = None  # type: ignore

    # --- Logging helpers ---
    def _log(self, message: str, level: str = 'info'):
        self.status_queue.put_nowait(message)
        getattr(self.logger, level, self.logger.info)(message)

    def _report_error(self, e: Exception, context: str):
        self._log(f"ERROR [{context}]: {e}", 'error')
        threading.Thread(target=send_discord_report, args=(e, context, self.log_file_path), daemon=True).start()

    # --- Debug HTTP tracing ---
    def _debug_patch_session(self):
        if self._orig_request_func is not None:
            return
        self._orig_request_func = self.session.request
        client = self
        def traced(method, url, **kwargs):  # type: ignore
            start = time.perf_counter()
            if client.debug:
                payload_desc = ''
                if 'json' in kwargs:
                    try:
                        payload_desc = f" json={str(kwargs['json'])[:500]}"
                    except Exception:
                        payload_desc = ' json=<unrepr>'
                elif 'data' in kwargs:
                    payload_desc = f" data={str(kwargs['data'])[:500]}"
                headers = kwargs.get('headers') or {}
                client._log(f"[HTTP ->] {method.upper()} {url}{payload_desc} headers={ {k: headers[k] for k in list(headers)[:10]} }")
            try:
                orig = client._orig_request_func or client.session.__class__.request
                resp = orig(method, url, **kwargs)
            except Exception as exc:
                dur = (time.perf_counter() - start) * 1000
                client._log(f"[HTTP !!] {method.upper()} {url} failed after {dur:.1f} ms: {exc}", 'error')
                raise
            dur = (time.perf_counter() - start) * 1000
            if client.debug:
                ct = resp.headers.get('Content-Type', '')
                size = len(resp.content or b'')
                snippet = ''
                if 'application/json' in ct:
                    try: snippet = resp.text[:1200]
                    except Exception: snippet = '<unreadable>'
                elif 'text/' in ct:
                    snippet = resp.text[:600]
                client._log(f"[HTTP <-] {method.upper()} {url} {resp.status_code} {size}B {dur:.1f}ms")
                if snippet and client.trace_http_bodies:
                    client._log(f"[HTTP BODY] {snippet}")
            return resp
        self.session.request = traced  # type: ignore

    def _debug_unpatch_session(self):
        if self._orig_request_func is not None:
            self.session.request = self._orig_request_func  # type: ignore
            self._orig_request_func = None

    # --- Browser Lifecycle ---
    def start_headless_browser(self):
        if self.driver:
            self._log("Browser is already running.", 'warning'); return
        if not getattr(self, 'service', None):
            self._log("Cannot start browser, ChromeService failed to initialize.", 'error'); return
        self._log("Starting browser...")
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-gpu")
        options.add_argument("--log-level=3")
        options.add_argument(f"user-agent={USER_AGENT}")
        options.add_experimental_option('excludeSwitches', ['enable-logging', 'enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument("--disable-blink-features=AutomationControlled")
        try:
            self.driver = webdriver.Chrome(service=self.service, options=options)
            self._log("Browser started successfully.")
        except Exception as e:
            self._log(f"Failed to start browser: {e}", 'error')
            self._report_error(e, 'during browser startup')
            self.driver = None

    # --- Auth ---
    def login(self, username, password) -> Tuple[bool, requests.Session | None]:
        if not self.driver:
            self._log("Browser not started.", 'error'); return False, None
        self._log(f"Attempting to log in as {username}...")
        try:
            self.driver.get(LOGIN_URL)
            if self.debug: self._log(f"[LOGIN] Navigated to login page: {self.driver.current_url}")
            WebDriverWait(self.driver, 10).until(EC.presence_of_element_located(USERNAME_FIELD_SELECTOR)).send_keys(username)
            self.driver.find_element(*PASSWORD_FIELD_SELECTOR).send_keys(password)
            if self.debug: self._log("[LOGIN] Credentials entered, clicking login button...")
            WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable(LOGIN_BUTTON_SELECTOR)).click()
            WebDriverWait(self.driver, 15).until(lambda d: d.find_elements(*LOGOUT_LINK_SELECTOR) or ("inloggen" not in d.current_url.lower()))
            self._log("Login successful.")
            if self.debug: self._log(f"Post-login URL: {self.driver.current_url}")
            # Copy cookies
            self.session.cookies.clear()
            for c in self.driver.get_cookies():
                if c.get('name'):
                    self.session.cookies.set(c['name'], c.get('value',''), domain=c.get('domain'), path=c.get('path','/'))
            self.session.headers.update({
                'User-Agent': USER_AGENT,
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': DISCOVERY_URL
            })
            try:
                probe = self.session.get(BASE_URL, timeout=10)
                if ("Uitloggen" not in probe.text) and ("Mijn Woonnet" not in probe.text):
                    self._log("Warning: Could not verify login via HTML markers.", 'warning')
            except Exception:
                pass
            self.is_logged_in = True
            return True, self.session
        except Exception as e:
            self._report_error(e, f"during login for user '{username}'")
            self.is_logged_in = False
            return False, None

    # --- Utility parse helpers ---
    def _parse_price(self, price_text: str) -> float:
        if not price_text: return 0.0
        return float(re.sub(r'[^\d,]', '', price_text).replace(',', '.'))

    def _parse_publ_date(self, date_str: str | None):
        if not date_str: return None
        m = re.search(r'/Date\((\d+)\)/', date_str)
        if m:
            try: return datetime.fromtimestamp(int(m.group(1)) / 1000.0)
            except Exception: return None
        try:
            s = date_str.replace('Z', '+00:00')
            dt = datetime.fromisoformat(s)
            return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
        except Exception: pass
        try: return datetime.strptime(date_str, "%B %d, %Y %H:%M:%S")
        except Exception: return None

    # --- Discovery & Details ---
    def discover_listings_api(self) -> List[Dict[str, Any]]:
        if not self.is_logged_in:
            self._log("Not logged in.", 'error'); return []
        self._log("Discovering listings via API...")
        try:
            payload = {
                "woonwens": {"Kenmerken": [{"waarde": "1", "geenVoorkeur": False, "kenmerkType": "24", "name": "objecttype"}], "BerekendDatumTijd": datetime.now().strftime("%Y-%m-%d")},
                "paginaNummer": 1, "paginaGrootte": 100, "filterMode": "AlleenNieuwVandaag"
            }
            if self.debug: self._log(f"[DISCOVER] POST {API_DISCOVERY_URL} payload={payload}")
            response = self.session.post(API_DISCOVERY_URL, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
            initial_listings = data.get('d', {}).get('resultaten', [])
            if self.debug: self._log(f"Discovery HTTP {response.status_code}; raw count={len(initial_listings)}")
            current_ids = {r.get('FrontendAdvertentieId') for r in initial_listings if r.get('FrontendAdvertentieId')}
            if not current_ids:
                self._empty_runs += 1
                if self._empty_runs <= 3 or self.debug:
                    self._log(f"API returned no listings (empty_runs={self._empty_runs}).")
                elif self._empty_runs % 10 == 0:
                    self._log(f"Still empty after {self._empty_runs} consecutive discovery attempts.")
                return []
            if current_ids == self._last_listing_ids:
                self._empty_runs = 0
                self._log(f"No change ({len(current_ids)} cached).")
                return []
            new_ids = current_ids - self._last_listing_ids
            disappeared = self._last_listing_ids - current_ids
            self._last_listing_ids = current_ids
            self._empty_runs = 0
            self._log(f"Change detected: +{len(new_ids)} / -{len(disappeared)} (total {len(current_ids)}). Fetching details...")
            self._persist_ids()
        except Exception as e:
            self._report_error(e, "during API listing discovery")
            return []

        processed: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_id = {executor.submit(self.get_listing_details, r['FrontendAdvertentieId']): r['FrontendAdvertentieId'] for r in initial_listings if r.get('FrontendAdvertentieId')}
            for future in as_completed(future_to_id):
                try:
                    item = future.result()
                    if not item: continue
                    if self.debug: self._log(f"[DETAIL] Got detail for ID {item.get('id')} keys={list(item)[:15]}")
                    now = datetime.now()
                    publ_start_dt = self._parse_publ_date(item.get('publstart'))
                    is_live = publ_start_dt and now >= publ_start_dt
                    is_in_window = now.hour >= APPLICATION_HOUR - 2
                    if is_live: status_text = "LIVE"
                    else:
                        start_time_str = publ_start_dt.strftime('%H:%M') if publ_start_dt else f"{APPLICATION_HOUR}:00"
                        status_text = f"SELECTABLE ({start_time_str})" if is_in_window else f"PREVIEW ({start_time_str})"
                    can_select = is_live or is_in_window
                    main_photo = next((m for m in item.get('media', []) if m.get('type') == 'StraatFoto'), None)
                    image_url = f"https:{main_photo['fotoviewer']}" if main_photo and main_photo.get('fotoviewer') else None
                    processed.append({
                        'id': item.get('id'),
                        'address': f"{item.get('straat','')} {item.get('huisnummer','')}",
                        'type': item.get('objecttype','N/A'),
                        'price_str': f"€ {item.get('kalehuur','0,00')}",
                        'price_float': self._parse_price(item.get('kalehuur','')),
                        'status_text': status_text,
                        'is_selectable': can_select,
                        'image_url': image_url
                    })
                except Exception as e:
                    self._report_error(e, f"processing details for listing ID {future_to_id[future]}")
        self._log(f"Processed {len(processed)} listings.")
        return sorted(processed, key=lambda x: x['price_float'])

    def get_listing_details(self, listing_id: str) -> Dict | None:
        payload = {"Id": listing_id, "VolgendeId": 0, "Filters": "gebruik!=Complex|nieuwab==True", "inschrijfnummerTekst": "", "Volgorde": "", "hash": ""}
        try:
            response = self.session.post(API_DETAILS_SINGLE_URL, json=payload, timeout=10)
            response.raise_for_status()
            return response.json().get('d', {}).get('Aanbod')
        except requests.RequestException:
            self._log(f"Could not get details for listing {listing_id}", 'warning')
            return None

    # --- Apply Workflow ---
    def _get_server_countdown_seconds(self) -> float | None:
        self._log("Fetching precise countdown from server API...")
        try:
            response = self.session.get(API_TIMER_URL, timeout=10)
            response.raise_for_status()
            data = response.json(); remaining_ms = int(data['resterendetijd'])
            if remaining_ms <= 0:
                self._log("Server countdown is zero or negative. Proceeding.", 'warning'); return 0.0
            seconds = remaining_ms / 1000.0
            self._log(f"Success! Server countdown: {seconds:.2f} seconds.")
            return seconds
        except Exception as e:
            self._log(f"API Error (Timer): {e}", 'error')
            return None

    def apply_to_listings(self, listing_ids: List[str]):
        if not self.is_logged_in or not self.session:
            self._log("Not logged in. Please log in before applying.", 'error'); return
        self._log("Waiting for the application window to open...")
        remaining = self._get_server_countdown_seconds()
        if remaining is not None and remaining > 0:
            end_time = time.monotonic() + remaining
            while not self.stop_event.is_set() and time.monotonic() < end_time:
                left = end_time - time.monotonic()
                self._log(f"Waiting for server... T-minus {time.strftime('%H:%M:%S', time.gmtime(left))}")
                time.sleep(1)
        else:
            self._log("Could not get server time or time is up. Falling back to 8 PM check.", 'warning')
            while not self.stop_event.is_set() and datetime.now().hour < APPLICATION_HOUR:
                self._log(f"Waiting for {APPLICATION_HOUR}:00 PM... Current time: {datetime.now().strftime('%H:%M:%S')}")
                time.sleep(1)
        if self.stop_event.is_set():
            self._log("Stop command received during wait. Aborting application.", 'warning'); return
        self._log("Application window is open! Applying to all selected listings IN PARALLEL...", 'warning')

        def _apply_task(listing_id: str) -> bool:
            apply_url = f"{BASE_URL}/reageren/{listing_id}"
            try:
                if self.debug: self._log(f"[APPLY] Fetching form page for {listing_id} -> {apply_url}")
                page_res = self.session.get(apply_url, timeout=15)
                action_url, payload = self._extract_postable_form(page_res.text, apply_url)
                if not action_url or '__RequestVerificationToken' not in payload:
                    self._log(f"({listing_id}) FAILED: Could not find postable form/token.", 'error'); return False
                if self.debug: self._log(f"[APPLY] Posting form for {listing_id} to {action_url} payload_keys={list(payload)[:10]}")
                submit_res = self.session.post(action_url, data=payload, headers={'Referer': apply_url}, timeout=15, allow_redirects=True)
                text = submit_res.text
                if any(s in text for s in ("Wij hebben uw reactie verwerkt", "U heeft al gereageerd", "Uw reactie is ontvangen")):
                    self._log(f"SUCCESS! Applied to listing {listing_id}.")
                    return True
                self._log(f"({listing_id}) FAILED: No success message. Snippet: {text[:300]}...", 'error'); return False
            except Exception as e:
                self._report_error(e, f"applying to listing ID {listing_id}")
                return False

        success = 0
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_apply_task, lid): lid for lid in listing_ids}
            for f in as_completed(futures):
                if f.result(): success += 1
        self._log(f"Finished. Applied to {success} of {len(listing_ids)} selected listings.")

    def _extract_postable_form(self, html: str, base_url: str) -> tuple[str | None, dict]:
        soup = BeautifulSoup(html, 'html.parser')
        forms = [f for f in soup.find_all('form') if f.find('input', {'name': '__RequestVerificationToken'})] or soup.find_all('form')
        if not forms: return None, {}
        form = forms[0]; payload: dict[str, str] = {}
        for inp in form.find_all('input'):
            name = inp.get('name');
            if not name or inp.has_attr('disabled'): continue
            itype = (inp.get('type') or 'text').lower()
            if itype in ('checkbox','radio'):
                if inp.has_attr('checked'): payload[name] = inp.get('value','on')
            else: payload[name] = inp.get('value','')
        for sel in form.find_all('select'):
            name = sel.get('name');
            if not name: continue
            opt = sel.find('option', selected=True) or sel.find('option')
            if opt: payload[name] = opt.get('value') or (opt.text or '').strip()
        for ta in form.find_all('textarea'):
            name = ta.get('name');
            if name: payload[name] = (ta.text or '').strip()
        if 'Command' not in payload:
            submit = (form.find('button', {'type':'submit'}) or form.find('button', {'name':'Command'}) or form.find('input', {'type':'submit'}))
            if submit: payload[submit.get('name') or 'Command'] = submit.get('value') or (submit.text or '').strip()
        action_url = urljoin(base_url, form.get('action') or base_url)
        return action_url, payload

    # --- Persistence ---
    def set_cache_path(self, directory: str): self.cache_path = directory
    def load_cached_ids(self):
        if not self.cache_path: return
        fp = os.path.join(self.cache_path, 'last_ids.json')
        try:
            if os.path.exists(fp):
                with open(fp,'r',encoding='utf-8') as f: data = json.load(f)
                if isinstance(data, list):
                    self._last_listing_ids = set(str(x) for x in data)
                    self._log(f"Loaded {len(self._last_listing_ids)} cached IDs.")
        except Exception as e:
            self._log(f"Could not load cached ids: {e}", 'warning')
    def _persist_ids(self):
        if not self.cache_path: return
        fp = os.path.join(self.cache_path, 'last_ids.json')
        try:
            with open(fp,'w',encoding='utf-8') as f: json.dump(sorted(self._last_listing_ids), f)
        except Exception: pass

    # --- Debug toggles ---
    def set_debug(self, enabled: bool):
        self.debug = enabled
        if enabled:
            try: self._log(f"[DEBUG] Environment: pid={os.getpid()} python={sys.version.split()[0]} cwd={os.getcwd()}")
            except Exception: pass
            self._debug_patch_session(); self._log("Debug mode ENABLED. HTTP tracing active.")
        else:
            self._debug_unpatch_session(); self._log("Debug mode disabled. HTTP tracing stopped.")
    def set_http_body_trace(self, enabled: bool):
        self.trace_http_bodies = enabled
        self._log(f"HTTP body tracing {'ENABLED' if enabled else 'disabled'}.")

    # --- Shutdown ---
    def quit(self):
        self._log("Shutting down client...")
        self.stop_event.set()
        if self.driver:
            try: self.driver.quit()
            except WebDriverException: pass
        self.driver = None; self.is_logged_in = False


# Legacy alias for compatibility
WoonnetBot = WoonnetClient

__all__ = ["WoonnetClient", "WoonnetBot"]
