"""Core automation layer: browser automation, API calls, application workflow.

Refactored from legacy bot.py. Public class: WoonnetClient (alias WoonnetBot for
backwards compatibility with older UI / scripts).
"""

from __future__ import annotations

import os, sys, time, re, json, threading, logging, requests
from datetime import datetime, timezone
from queue import Queue
from typing import List, Dict, Any, Tuple, Optional, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup
from bs4.element import Tag
from typing import cast
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
    LOGIN_BUTTON_SELECTOR, LOGOUT_LINK_SELECTOR, USER_AGENT, APPLICATION_HOUR,
    PRE_SELECTION_HOUR, PRE_SELECTION_MINUTE, FINAL_REFRESH_HOUR, FINAL_REFRESH_MINUTE
)

if not getattr(sys, 'frozen', False):  # only import manager when not frozen
    from webdriver_manager.chrome import ChromeDriverManager  # type: ignore


class WoonnetClient:
    """Automates interaction with WoonnetRijnmond website (login, discover, apply).

    This class encapsulates all network / browser side-effects so the rest of the
    application (UI, tests) can treat it as a service.

    Parameters
    ----------
    status_queue : Queue
        Destination for user-friendly log / status messages (e.g. UI thread).
    logger : logging.Logger
        Structured logger for file / console output.
    log_file_path : str
        Path to primary rolling log file (attached to Discord reports).
    enable_browser : bool, default True
        Skip Selenium initialization when False (or when env var WOONNET_NO_BROWSER=1).
        This is critical for fast CI/unit tests that only exercise parsing / HTTP logic.
    max_workers : int, default 10
        Thread pool size for parallel detail fetching & application submission.
    """
    def __init__(
        self,
        status_queue: Queue | None,
        logger: logging.Logger,
        log_file_path: str,
        *,
        enable_browser: bool = True,
        max_workers: int = 10,
    ) -> None:
        self.driver = None  # webdriver.Chrome | None
        self.status_queue = status_queue
        self.logger = logger
        self.log_file_path = log_file_path
        self.stop_event = threading.Event()
        self.session = requests.Session()
        # Concurrency configuration early (used for HTTP adapter sizing)
        self.max_workers = max(1, max_workers)

        # HTTP adapter with retry & pool sizing
        from requests.adapters import HTTPAdapter
        try:
            from urllib3.util.retry import Retry  # type: ignore
        except Exception:
            Retry = None  # type: ignore
        pool_sz = max(32, self.max_workers * 2)
        adapter = HTTPAdapter(
            pool_connections=pool_sz,
            pool_maxsize=pool_sz,
            max_retries=Retry(total=2, backoff_factor=0.25, status_forcelist=[500, 502, 503, 504]) if Retry else 0
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # State / instrumentation
        self.is_logged_in = False
        self.debug = False
        self.trace_http_bodies = True
        self._last_listing_ids: set[str] = set()
        self._empty_runs = 0
        self.cache_path = None
        self._orig_request_func = None
        # Metrics (thread-safe counters)
        self._metrics_lock = threading.Lock()
        self._metrics = {
            'http_requests': 0,
            'http_errors': 0,
            'http_total_ms': 0.0,
            'discovery_runs': 0,
            'last_discovery_ms': 0.0,
            'last_change_new': 0,
            'last_change_disappeared': 0,
            'last_listings_processed': 0,
            'detail_fetch_total_ms': 0.0,
            'detail_fetch_items': 0,
            'apply_attempts': 0,
            'apply_success': 0,
        }

        # Respect env override for browser
        if os.environ.get("WOONNET_NO_BROWSER") == "1":
            enable_browser = False

        self.service = None
        if enable_browser:
            try:
                if getattr(sys, 'frozen', False):
                    base_dir = os.path.dirname(sys.executable)
                    candidate_paths = [
                        os.path.join(base_dir, 'chromedriver.exe'),
                        os.path.join(getattr(sys, '_MEIPASS', base_dir), 'chromedriver.exe')  # type: ignore
                    ]
                    driver_path = next((p for p in candidate_paths if os.path.exists(p)), None)
                    if not driver_path:
                        try:
                            import urllib.request, zipfile, io, json as _json
                            self._log("Chromedriver not bundled; attempting runtime download of latest stable...")
                            meta_url = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
                            with urllib.request.urlopen(meta_url, timeout=15) as r:
                                meta = _json.loads(r.read().decode())
                            dl = next(d for d in meta['channels']['Stable']['downloads']['chromedriver'] if d['platform'] == 'win64')
                            with urllib.request.urlopen(dl['url'], timeout=30) as r:
                                zbytes = r.read()
                            with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
                                member = next(m for m in zf.namelist() if m.endswith('chromedriver.exe'))
                                with open(os.path.join(base_dir, 'chromedriver.exe'), 'wb') as f:
                                    f.write(zf.read(member))
                            driver_path = os.path.join(base_dir, 'chromedriver.exe')
                            self._log(f"Downloaded chromedriver to {driver_path}")
                        except Exception as de:
                            self._log(f"Failed to download chromedriver: {de}", 'error')
                            driver_path = None
                    if not driver_path:
                        raise RuntimeError('Chromedriver executable not found.')
                    self.service = ChromeService(executable_path=driver_path)
                    self._log(f"Runtime mode: Using chromedriver from {driver_path}")
                else:
                    self._log("Script mode: Installing/updating chromedriver with webdriver-manager...")
                    self.service = ChromeService(ChromeDriverManager().install())  # type: ignore
            except Exception as e:
                self._log(f"CRITICAL: Failed to initialize ChromeService: {e}", "error")
                self._report_error(e, "during ChromeService initialization")
                self.service = None  # type: ignore

    # --- Logging helpers ---
    def _log(self, message: str, level: str = 'info') -> None:
        if self.status_queue is not None:
            try:
                self.status_queue.put_nowait(message)
            except Exception:
                pass
        getattr(self.logger, level, self.logger.info)(message)

    def _report_error(self, e: Exception, context: str) -> None:
        self._log(f"ERROR [{context}]: {e}", 'error')
        threading.Thread(target=send_discord_report, args=(e, context, self.log_file_path), daemon=True).start()

    # --- Debug HTTP tracing ---
    def _debug_patch_session(self) -> None:
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
                with client._metrics_lock:
                    client._metrics['http_requests'] += 1
                    client._metrics['http_errors'] += 1
                    client._metrics['http_total_ms'] += dur
                raise
            dur = (time.perf_counter() - start) * 1000
            with client._metrics_lock:
                client._metrics['http_requests'] += 1
                client._metrics['http_total_ms'] += dur
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

    def _debug_unpatch_session(self) -> None:
        if self._orig_request_func is not None:
            self.session.request = self._orig_request_func  # type: ignore
            self._orig_request_func = None

    # --- Browser Lifecycle ---
    def start_headless_browser(self) -> None:
        if self.driver:
            self._log("Browser is already running.", 'warning'); return
        if not getattr(self, 'service', None):
            self._log("Cannot start browser, ChromeService failed to initialize.", 'error'); return
        self._log("Starting browser...")
        options = webdriver.ChromeOptions()
        defender_safe = os.environ.get("DEFENDER_SAFE_MODE") == "1"
        # Minimal set of flags in defender-safe mode to reduce heuristic triggers
        if defender_safe:
            options.add_argument("--headless")  # avoid "new" flavor
            options.add_argument(f"user-agent={USER_AGENT}")
        else:
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-gpu")
            options.add_argument("--log-level=3")
            options.add_argument(f"user-agent={USER_AGENT}")
            # The following anti-detection tweaks can elevate heuristic risk; omit when safe mode enabled
            options.add_experimental_option('excludeSwitches', ['enable-logging', 'enable-automation'])
            options.add_experimental_option('useAutomationExtension', False)
            options.add_argument("--disable-blink-features=AutomationControlled")
        if defender_safe:
            self._log("DEFENDER_SAFE_MODE=1 active: using reduced Chrome flags.")
        try:
            self.driver = webdriver.Chrome(service=self.service, options=options)
            self._log("Browser started successfully.")
        except Exception as e:
            self._log(f"Failed to start browser: {e}", 'error')
            self._report_error(e, 'during browser startup')
            self.driver = None

    # --- Auth ---
    def login(self, username: str, password: str) -> Tuple[bool, Optional[requests.Session]]:
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
            # Persist cookies after a successful login so future runs can reuse them.
            try:
                self.save_cookies()
            except Exception:
                pass
            return True, self.session
        except Exception as e:
            self._report_error(e, f"during login for user '{username}'")
            self.is_logged_in = False
            return False, None

    # --- Utility parse helpers ---
    def _parse_price(self, price_text: str) -> float:
        if not price_text: return 0.0
        return float(re.sub(r'[^\d,]', '', price_text).replace(',', '.'))

    def _parse_publ_date(self, date_str: Optional[str]) -> Optional[datetime]:
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

    # --- Internal helpers ---
    def _fetch_discovery_pages(self, base_payload: dict, *, max_pages: int = 20) -> list[dict]:
        """Fetch all discovery pages until exhaustion or max_pages.

        Emulates client-side pagination the site would perform. Stops early when a page
        returns fewer results than requested or an empty list. Any HTTP error bubbles
        up to caller to preserve existing error reporting semantics there.
        """
        all_results: list[dict] = []
        per_page = int(base_payload.get("paginaGrootte", 100) or 100)
        for page in range(1, max_pages + 1):
            payload = dict(base_payload)
            payload["paginaNummer"] = page
            r = self.session.post(API_DISCOVERY_URL, json=payload, timeout=15)
            r.raise_for_status()
            data = r.json() or {}
            batch = (data.get('d') or {}).get('resultaten') or []
            if self.debug:
                self._log(f"[DISCOVER:PAGINATION] page={page} batch={len(batch)} total_so_far={len(all_results)}")
            if not batch:
                break
            all_results.extend(batch)
            if len(batch) < per_page:  # last page (short)
                break
        return all_results

    # --- Discovery & Details ---
    def discover_listings_api(self) -> List[Dict[str, Any]]:
        if not self.is_logged_in:
            self._log("Not logged in.", 'error'); return []
        self._log("Discovering listings via API...")
        overall_start = time.perf_counter()
        try:
            payload = {
                "woonwens": {
                    "Kenmerken": [{"waarde": "1", "geenVoorkeur": False, "kenmerkType": "24", "name": "objecttype"}],
                    "BerekendDatumTijd": datetime.now().strftime("%Y-%m-%d"),
                },
                "paginaNummer": 1,
                "paginaGrootte": 100,
                "filterMode": "AlleenNieuwVandaag",
            }
            if self.debug:
                self._log(f"[DISCOVER] BEGIN pagination payload={payload}")
            initial_listings = self._fetch_discovery_pages(payload, max_pages=25)
            if self.debug:
                self._log(f"[DISCOVER] Aggregated listings count={len(initial_listings)}")
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
            # Normalize to set[str] to satisfy type checking
            self._last_listing_ids = {str(x) for x in current_ids if x is not None}
            self._empty_runs = 0
            self._log(f"Change detected: +{len(new_ids)} / -{len(disappeared)} (total {len(current_ids)}). Fetching details...")
            with self._metrics_lock:
                self._metrics['last_change_new'] = len(new_ids)
                self._metrics['last_change_disappeared'] = len(disappeared)
            self._persist_ids()
        except Exception as e:
            self._report_error(e, "during API listing discovery")
            return []
        processed: List[Dict[str, Any]] = []
        # Allow user-configurable thread pool size for experimentation / tuning.
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_id = {executor.submit(self.get_listing_details, r['FrontendAdvertentieId']): r['FrontendAdvertentieId'] for r in initial_listings if r.get('FrontendAdvertentieId')}
            for future in as_completed(future_to_id):
                try:
                    item = future.result()
                    if not item:
                        continue
                    if self.debug:
                        self._log(f"[DETAIL] Got detail for ID {item.get('id')} keys={list(item)[:15]}")
                    # --- Status / window computation using precise timing constants ---
                    now = datetime.now()
                    publ_start_dt = self._parse_publ_date(item.get('publstart'))
                    from datetime import time as _time
                    pre_sel = _time(hour=PRE_SELECTION_HOUR, minute=PRE_SELECTION_MINUTE)
                    final_refresh = _time(hour=FINAL_REFRESH_HOUR, minute=FINAL_REFRESH_MINUTE)  # reserved for potential future nuanced states
                    app_time = _time(hour=APPLICATION_HOUR, minute=0)
                    t_now = now.time()
                    is_live = bool(publ_start_dt and now >= publ_start_dt)
                    is_in_window = (pre_sel <= t_now <= app_time)
                    if is_live:
                        status_text = "LIVE"
                    else:
                        start_time_str = publ_start_dt.strftime('%H:%M') if publ_start_dt else f"{APPLICATION_HOUR:02d}:00"
                        status_text = f"SELECTABLE ({start_time_str})" if is_in_window else f"PREVIEW ({start_time_str})"
                    can_select = is_live or is_in_window
                    media_items = item.get('media', []) or []
                    main_photo = next((m for m in media_items if m.get('type') == 'StraatFoto'), None)
                    image_url = f"https:{main_photo['fotoviewer']}" if main_photo and main_photo.get('fotoviewer') else None
                    # Collect additional images (unique order preserved) – cap to first 8 for performance
                    image_urls: list[str] = []
                    for m in media_items:
                        fv = m.get('fotoviewer')
                        if fv:
                            full = f"https:{fv}" if fv.startswith("//") else fv
                            if full not in image_urls:
                                image_urls.append(full)
                        if len(image_urls) >= 8:
                            break
                    # Heuristic extraction of commonly displayed metadata + preservation of raw detail
                    def _pick(d: dict, *candidates: str):
                        # tolerant lookups (exact, then case-insensitive contains)
                        for k in candidates:
                            if k in d and d[k] not in (None, "", "?"):
                                return d[k]
                        dl = {str(k).lower(): k for k in d.keys()}
                        for lk, orig in dl.items():
                            for want in candidates:
                                if want.lower() in lk and d[orig] not in (None, "", "?"):
                                    return d[orig]
                        return None

                    rooms_val = _pick(item, "kamers", "aantalKamers", "AantalKamers", "kamersAantal")
                    surf_val  = _pick(item, "oppervlakte", "WoonOppervlakte", "m2", "m\u00b2")
                    city      = _pick(item, "plaats", "woonplaats", "stad")
                    postcode  = _pick(item, "postcode")
                    energylbl = _pick(item, "energielabel", "energieLabel")
                    service   = _pick(item, "servicekosten", "ServiceKosten", "service_kosten")

                    meta_pairs = []
                    for label, val in (
                        ("Rooms", rooms_val),
                        ("Area", f"{surf_val} m²" if surf_val else None),
                        ("City", city),
                        ("Postcode", postcode),
                        ("Energy", energylbl),
                        ("Service costs", f"€ {service}" if service else None),
                    ):
                        if val:
                            meta_pairs.append((label, str(val)))

                    processed.append({
                        'id': item.get('id'),
                        'address': f"{item.get('straat','')} {item.get('huisnummer','')}",
                        'type': item.get('objecttype','N/A'),
                        'price_str': f"€ {item.get('kalehuur','0,00')}",
                        'price_float': self._parse_price(item.get('kalehuur','')),
                        'status_text': status_text,
                        'is_selectable': can_select,
                        'image_url': image_url,
                        'image_urls': image_urls,
                        'rooms': rooms_val,
                        'area_m2': surf_val,
                        'city': city,
                        'postcode': postcode,
                        'energy_label': energylbl,
                        'service_costs': service,
                        'meta_pairs': meta_pairs,
                        'raw_detail': item,
                    })
                    with self._metrics_lock:
                        self._metrics['detail_fetch_items'] += 1
                except Exception as e:
                    self._report_error(e, f"processing details for listing ID {future_to_id[future]}")
        self._log(f"Processed {len(processed)} listings.")
        elapsed_ms = (time.perf_counter() - overall_start) * 1000
        with self._metrics_lock:
            self._metrics['discovery_runs'] += 1
            self._metrics['last_discovery_ms'] = elapsed_ms
            self._metrics['last_listings_processed'] = len(processed)
        if self.debug:
            avg_http = (self._metrics['http_total_ms'] / self._metrics['http_requests']) if self._metrics['http_requests'] else 0.0
            self._log(f"[DISCOVER] Duration {elapsed_ms:.1f} ms avg_http={avg_http:.1f} ms")
        return sorted(processed, key=lambda x: x['price_float'])

    def get_listing_details(self, listing_id: str) -> Optional[Dict]:
        payload = {"Id": listing_id, "VolgendeId": 0, "Filters": "gebruik!=Complex|nieuwab==True", "inschrijfnummerTekst": "", "Volgorde": "", "hash": ""}
        start = time.perf_counter()
        try:
            response = self.session.post(API_DETAILS_SINGLE_URL, json=payload, timeout=10)
            response.raise_for_status()
            return response.json().get('d', {}).get('Aanbod')
        except requests.RequestException:
            self._log(f"Could not get details for listing {listing_id}", 'warning')
            return None
        finally:
            dur = (time.perf_counter() - start) * 1000
            with self._metrics_lock:
                self._metrics['detail_fetch_total_ms'] += dur

    # --- Apply Workflow ---
    def _get_server_countdown_seconds(self) -> Optional[float]:
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

    def apply_to_listings(self, listing_ids: Sequence[str]) -> None:
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
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(_apply_task, lid): lid for lid in listing_ids}
            for f in as_completed(futures):
                ok = False
                try:
                    ok = bool(f.result())
                finally:
                    with self._metrics_lock:
                        self._metrics['apply_attempts'] += 1
                        if ok:
                            self._metrics['apply_success'] += 1
                if ok:
                    success += 1

        self._log(f"Finished. Applied to {success} of {len(listing_ids)} selected listings.")

    def _extract_postable_form(self, html: str, base_url: str) -> Tuple[Optional[str], dict]:
        """Extract a likely application form and construct payload.

        Type-checker friendly: avoid using BeautifulSoup dynamic attributes directly
        without casting; coerce attribute values to str.
        """
        soup = BeautifulSoup(html, 'html.parser')
        forms = list(soup.find_all('form'))
        if not forms:
            return None, {}
        # Prefer a form containing the anti-forgery token
        chosen: Optional[Tag] = None
        for f in forms:
            if isinstance(f, Tag) and f.find('input', {'name': '__RequestVerificationToken'}):
                chosen = cast(Tag, f)
                break
        if chosen is None:
            chosen = cast(Tag, forms[0])
        form = chosen
        payload: dict[str, str] = {}

        def _as_str(val: object, default: str = '') -> str:
            if isinstance(val, str):
                return val
            return default if val is None else str(val)

        # Inputs
        for inp in form.find_all('input'):
            if not isinstance(inp, Tag):
                continue
            name = _as_str(inp.get('name'))
            if not name or inp.get('disabled') is not None:
                continue
            itype = _as_str(inp.get('type'), 'text').lower()
            if itype in ('checkbox', 'radio'):
                if inp.get('checked') is not None:
                    payload[name] = _as_str(inp.get('value'), 'on') or 'on'
            else:
                payload[name] = _as_str(inp.get('value'))

        # Selects
        for sel in form.find_all('select'):
            if not isinstance(sel, Tag):
                continue
            sel_name = _as_str(sel.get('name'))
            if not sel_name:
                continue
            opt = sel.find('option', {'selected': True}) or sel.find('option')
            if isinstance(opt, Tag):
                payload[sel_name] = _as_str(opt.get('value')) or (opt.text or '').strip()

        # Textareas
        for ta in form.find_all('textarea'):
            if not isinstance(ta, Tag):
                continue
            ta_name = _as_str(ta.get('name'))
            if ta_name:
                payload[ta_name] = (ta.text or '').strip()

        # Submit / command fallback
        if 'Command' not in payload:
            submit = (
                form.find('button', {'type': 'submit'})
                or form.find('button', {'name': 'Command'})
                or form.find('input', {'type': 'submit'})
            )
            if isinstance(submit, Tag):
                submit_name = _as_str(submit.get('name'), 'Command') or 'Command'
                submit_val = _as_str(submit.get('value')) or (submit.text or '').strip()
                payload[submit_name] = submit_val

        action = _as_str(form.get('action')) or base_url
        return urljoin(base_url, action), payload

    # --- Persistence ---
    def set_cache_path(self, directory: str) -> None:
        """Define where ID cache persistence will read/write.

        The file `last_ids.json` will be stored in this directory.
        """
        self.cache_path = directory
        # Load any previously saved session cookies when a cache path becomes available.
        self.load_cookies()

    def load_cached_ids(self) -> None:
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
    def _persist_ids(self) -> None:
        if not self.cache_path: return
        fp = os.path.join(self.cache_path, 'last_ids.json')
        try:
            with open(fp,'w',encoding='utf-8') as f: json.dump(sorted(str(x) for x in self._last_listing_ids), f)
        except Exception: pass

    # --- Cookie persistence ---
    def _cookies_path(self) -> Optional[str]:
        return os.path.join(self.cache_path, 'cookies.json') if self.cache_path else None

    def save_cookies(self) -> None:
        """Serialize current session cookies to disk (best-effort)."""
        try:
            p = self._cookies_path()
            if not p:
                return
            from requests.utils import dict_from_cookiejar
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(dict_from_cookiejar(self.session.cookies), f)
            self._log(f"Saved cookies to {p}")
        except Exception as e:
            self._log(f"Could not save cookies: {e}", 'warning')

    def load_cookies(self) -> None:
        """Load previously persisted cookies into the session (best-effort)."""
        try:
            p = self._cookies_path()
            if not p or not os.path.exists(p):
                return
            from requests.utils import cookiejar_from_dict
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.session.cookies = cookiejar_from_dict(data)
            self._log(f"Loaded cookies from {p}")
        except Exception as e:
            self._log(f"Could not load cookies: {e}", 'warning')
    
    def get_cached_listing_ids(self) -> List[str]:
        """Return a snapshot list of the last discovered listing IDs (for tests / diagnostics)."""
        return sorted(str(x) for x in self._last_listing_ids)

    # --- Debug toggles ---
    def set_debug(self, enabled: bool) -> None:
        self.debug = enabled
        if enabled:
            try: self._log(f"[DEBUG] Environment: pid={os.getpid()} python={sys.version.split()[0]} cwd={os.getcwd()}")
            except Exception: pass
            self._debug_patch_session(); self._log("Debug mode ENABLED. HTTP tracing active.")
        else:
            self._debug_unpatch_session(); self._log("Debug mode disabled. HTTP tracing stopped.")
    def set_http_body_trace(self, enabled: bool) -> None:
        self.trace_http_bodies = enabled
        self._log(f"HTTP body tracing {'ENABLED' if enabled else 'disabled'}.")

    # --- Metrics API ---
    def get_metrics(self) -> Dict[str, Any]:
        """Snapshot of internal metrics with derived averages for UI display."""
        with self._metrics_lock:
            snap = dict(self._metrics)
        snap['avg_http_ms'] = (snap['http_total_ms'] / snap['http_requests']) if snap['http_requests'] else 0.0
        snap['avg_detail_fetch_ms'] = (snap['detail_fetch_total_ms'] / snap['detail_fetch_items']) if snap['detail_fetch_items'] else 0.0
        return snap

    # --- Shutdown ---
    def quit(self) -> None:
        self._log("Shutting down client...")
        self.stop_event.set()
        if self.driver:
            try: self.driver.quit()
            except WebDriverException: pass
        self.driver = None; self.is_logged_in = False


# Legacy alias for compatibility
WoonnetBot = WoonnetClient

__all__ = ["WoonnetClient", "WoonnetBot"]
