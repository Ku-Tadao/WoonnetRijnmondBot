# bot.py
# -*- coding: utf-8 -*-
""" WoonnetBot Core Logic ... """

import time, re, sys, os, threading, requests, logging
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

# Only import webdriver_manager if not running as a frozen executable
if not getattr(sys, 'frozen', False):
    from webdriver_manager.chrome import ChromeDriverManager

# MODIFIED: Import from new reporting module and more from config
from reporting import send_discord_report
from config import (
    BASE_URL, LOGIN_URL, DISCOVERY_URL, API_DISCOVERY_URL, API_DETAILS_SINGLE_URL,
    API_TIMER_URL, USERNAME_FIELD_SELECTOR, PASSWORD_FIELD_SELECTOR,
    LOGIN_BUTTON_SELECTOR, LOGOUT_LINK_SELECTOR, USER_AGENT, APPLICATION_HOUR
)


class WoonnetBot:
    """ Manages all automated interactions with the WoonnetRijnmond website. """
    def __init__(self, status_queue: Queue, logger: logging.Logger, log_file_path: str): # MODIFIED
        self.driver: webdriver.Chrome | None = None
        self.status_queue = status_queue
        self.logger = logger
        self.log_file_path = log_file_path # NEW: Keep track of the log file for reporting
        self.stop_event = threading.Event()
        self.session = requests.Session()
        self.is_logged_in = False

        # MODIFIED: This logic is now critical for the EXE to work.
        # The .spec file ensures chromedriver.exe is in sys._MEIPASS.
        try:
            if getattr(sys, 'frozen', False):
                # When running as a compiled executable (frozen)
                driver_path = os.path.join(sys._MEIPASS, "chromedriver.exe") # type: ignore
                self.service = ChromeService(executable_path=driver_path)
                self._log(f"Bundle mode: Using chromedriver from {driver_path}")
            else:
                # When running as a script
                self._log("Script mode: Installing/updating chromedriver with webdriver-manager...")
                self.service = ChromeService(ChromeDriverManager().install()) # type: ignore
        except Exception as e:
            self._log(f"CRITICAL: Failed to initialize ChromeService: {e}", "error")
            self._report_error(e, "during ChromeService initialization")
            self.service = None # type: ignore

    def _log(self, message: str, level: str = 'info'):
        """ Sends a log message to both the GUI status queue and the logger. """
        self.status_queue.put_nowait(message)
        getattr(self.logger, level, self.logger.info)(message)

    # NEW: Centralized error reporting method
    def _report_error(self, e: Exception, context: str):
        """ Logs the error and sends a report to Discord. """
        self._log(f"ERROR [{context}]: {e}", "error")
        # Run in a separate thread to not block the bot
        threading.Thread(target=send_discord_report, args=(e, context, self.log_file_path), daemon=True).start()

    def start_headless_browser(self):
        """ Initializes and starts the Selenium WebDriver instance. """
        if self.driver:
            self._log("Browser is already running.", "warning")
            return
        if not self.service:
            self._log("Cannot start browser, ChromeService failed to initialize.", "error")
            return
            
        self._log("Starting browser...")
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-gpu")
        options.add_argument("--log-level=3")
        options.add_argument(f"user-agent={USER_AGENT}") # NEW: Use consistent user agent
        # Stealth-ish flags
        options.add_experimental_option('excludeSwitches', ['enable-logging', 'enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument("--disable-blink-features=AutomationControlled")

        try:
            self.driver = webdriver.Chrome(service=self.service, options=options)
            self._log("Browser started successfully.")
        except Exception as e:
            self._log(f"Failed to start browser: {e}", level='error')
            self._report_error(e, "during browser startup")
            self.driver = None

    def login(self, username, password) -> Tuple[bool, requests.Session | None]:
        """ Performs login using Selenium and transfers the session to requests. """
        if not self.driver:
            self._log("Browser not started.", 'error')
            return False, None
        self._log(f"Attempting to log in as {username}...")
        try:
            self.driver.get(LOGIN_URL)
            WebDriverWait(self.driver, 10).until(EC.presence_of_element_located(USERNAME_FIELD_SELECTOR)).send_keys(username)
            self.driver.find_element(*PASSWORD_FIELD_SELECTOR).send_keys(password)
            WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable(LOGIN_BUTTON_SELECTOR)).click()
            # Consider login successful if logout appears OR URL changes away from the login page
            WebDriverWait(self.driver, 15).until(lambda d: d.find_elements(*LOGOUT_LINK_SELECTOR) or ("inloggen" not in d.current_url.lower()))
            self._log("Login successful.")

            # Copy cookies into requests (domain + path)
            self.session.cookies.clear()
            for c in self.driver.get_cookies():
                if c.get('name'):
                    self.session.cookies.set(
                        c['name'], c.get('value',''),
                        domain=c.get('domain'),
                        path=c.get('path','/')
                    )


            self.session.headers.update({
                'User-Agent': USER_AGENT,
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'X-Requested-With': 'XMLHttpRequest',
                'Referer': DISCOVERY_URL
            })
            # Optional server-side verification of logged-in state
            try:
                probe = self.session.get(BASE_URL, timeout=10)
                if ("Uitloggen" not in probe.text) and ("Mijn Woonnet" not in probe.text):
                    self._log("Warning: Could not verify login via HTML markers.", "warning")
            except Exception:
                pass
            self.is_logged_in = True
            return True, self.session
        except Exception as e:
            self._report_error(e, f"during login for user '{username}'")
            self.is_logged_in = False
            return False, None

    # ... (the _get_server_countdown_seconds, _parse_price, _parse_publ_date methods remain the same)
    def _get_server_countdown_seconds(self) -> float | None:
        """ Fetches the official countdown time from the server's API. """
        self._log("Fetching precise countdown from server API...")
        try:
            response = self.session.get(API_TIMER_URL, timeout=10)
            response.raise_for_status()
            data = response.json()
            remaining_ms = int(data['resterendetijd'])
            if remaining_ms <= 0:
                self._log("Server countdown is zero or negative. Proceeding.", "warning")
                return 0.0
            seconds = remaining_ms / 1000.0
            self._log(f"Success! Server countdown: {seconds:.2f} seconds.")
            return seconds
        except requests.RequestException as e:
            self._log(f"API Error (Timer): Could not get official time. {e}", "error")
            return None
        except (KeyError, ValueError, TypeError) as e:
            self._log(f"API Error (Timer): Could not parse server response. {e}", "error")
            return None

    def _extract_postable_form(self, html: str, base_url: str) -> tuple[str | None, dict]:
        """Find a likely application form, build a payload with inputs/checkboxes/radios/selects/textareas, and resolve action URL."""
        soup = BeautifulSoup(html, 'html.parser')
        forms = [f for f in soup.find_all('form')
                 if f.find('input', {'name': '__RequestVerificationToken'})] or soup.find_all('form')
        if not forms:
            return None, {}
        form = forms[0]
        payload: dict[str, str] = {}
        # Inputs
        for inp in form.find_all('input'):
            name = inp.get('name')
            if not name or inp.has_attr('disabled'):
                continue
            itype = (inp.get('type') or 'text').lower()
            if itype in ('checkbox', 'radio'):
                if inp.has_attr('checked'):
                    payload[name] = inp.get('value', 'on')
            else:
                payload[name] = inp.get('value', '')
        # Selects
        for sel in form.find_all('select'):
            name = sel.get('name')
            if not name:
                continue
            opt = sel.find('option', selected=True) or sel.find('option')
            if opt:
                payload[name] = opt.get('value') or (opt.text or '').strip()
        # Textareas
        for ta in form.find_all('textarea'):
            name = ta.get('name')
            if name:
                payload[name] = (ta.text or '').strip()
        # Submit fallback
        if 'Command' not in payload:
            submit = (form.find('button', {'type': 'submit'}) or
                      form.find('button', {'name': 'Command'}) or
                      form.find('input', {'type': 'submit'}))
            if submit:
                payload[submit.get('name') or 'Command'] = submit.get('value') or (submit.text or '').strip()
        action = form.get('action') or base_url
        action_url = urljoin(base_url, action)
        return action_url, payload

    def apply_to_listings(self, listing_ids: List[str]):
        """ Applies to a list of properties by sending direct POST requests in parallel. """
        if not self.is_logged_in or not self.session:
            self._log("Not logged in. Please log in before applying.", 'error')
            return

        self._log("Waiting for the application window to open...")
        remaining_seconds = self._get_server_countdown_seconds()
        
        # MODIFIED: Simplified countdown logic
        if remaining_seconds is not None and remaining_seconds > 0:
            end_time = time.monotonic() + remaining_seconds
            while not self.stop_event.is_set() and time.monotonic() < end_time:
                time_left = end_time - time.monotonic()
                timer_display = time.strftime('%H:%M:%S', time.gmtime(time_left))
                self._log(f"Waiting for server... T-minus {timer_display}")
                time.sleep(1)
        else:
            self._log("Could not get server time or time is up. Falling back to 8 PM check.", "warning")
            while not self.stop_event.is_set():
                if datetime.now().hour >= APPLICATION_HOUR: break
                self._log(f"Waiting for {APPLICATION_HOUR}:00 PM... Current time: {datetime.now().strftime('%H:%M:%S')}")
                time.sleep(1)

        if self.stop_event.is_set():
            self._log("Stop command received during wait. Aborting application.", "warning")
            return

        self._log("Application window is open! Applying to all selected listings IN PARALLEL...", 'warning')
        
        def _apply_task(listing_id: str) -> bool:
            """ Worker function to apply to a single listing. """
            apply_url = f"{BASE_URL}/reageren/{listing_id}"
            try:
                page_res = self.session.get(apply_url, timeout=15)
                action_url, payload = self._extract_postable_form(page_res.text, apply_url)
                if not action_url or '__RequestVerificationToken' not in payload:
                    self._log(f"({listing_id}) FAILED: Could not find postable form/token.", 'error')
                    return False
                submit_res = self.session.post(
                    action_url, data=payload,
                    headers={'Referer': apply_url},
                    timeout=15, allow_redirects=True
                )
                text = submit_res.text
                if any(s in text for s in ("Wij hebben uw reactie verwerkt",
                                            "U heeft al gereageerd",
                                            "Uw reactie is ontvangen")):
                    self._log(f"SUCCESS! Applied to listing {listing_id}.", 'info')
                    return True
                else:
                    self._log(f"({listing_id}) FAILED: No success message. Snippet: {text[:300]}...", 'error')
                    return False
            except Exception as e:
                # NEW: Report errors from within the application thread
                self._report_error(e, f"applying to listing ID {listing_id}")
                return False

        success_count = 0
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_id = {executor.submit(_apply_task, lid): lid for lid in listing_ids}
            for future in as_completed(future_to_id):
                if future.result():
                    success_count += 1
    
        self._log(f"Finished. Applied to {success_count} of {len(listing_ids)} selected listings.")

    # ... The quit, _parse_price, _parse_publ_date methods remain the same ...
    def quit(self):
        """ Safely shuts down the bot, closing the browser and stopping threads. """
        self._log("Shutting down bot instance...")
        self.stop_event.set()
        if self.driver:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
        self.driver = None
        self.is_logged_in = False

    def _parse_price(self, price_text: str) -> float:
        """Utility to convert a formatted price string to a float."""
        if not price_text: return 0.0
        return float(re.sub(r'[^\d,]', '', price_text).replace(',', '.'))

    def _parse_publ_date(self, date_str: str | None) -> datetime | None:
        """Parse publication date: supports /Date(…)/, ISO-8601 (with Z), and legacy string."""
        if not date_str:
            return None
        # /Date(1692052500000)/
        m = re.search(r'/Date\((\d+)\)/', date_str)
        if m:
            try:
                return datetime.fromtimestamp(int(m.group(1)) / 1000.0)
            except Exception:
                return None
        # ISO-8601 (possibly with Z)
        try:
            s = date_str.replace('Z', '+00:00')
            dt = datetime.fromisoformat(s)
            return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
        except Exception:
            pass
        # Fallback legacy format
        try:
            return datetime.strptime(date_str, "%B %d, %Y %H:%M:%S")
        except Exception:
            return None

    def discover_listings_api(self) -> List[Dict[str, Any]]:
        """ Discovers new listings for the day using the website's internal API. """
        if not self.is_logged_in:
            self._log("Not logged in.", 'error')
            return []
        self._log("Discovering listings via API...")
        try:
            payload = {
                "woonwens": {
                    "Kenmerken": [{"waarde": "1", "geenVoorkeur": False, "kenmerkType": "24", "name": "objecttype"}],
                    "BerekendDatumTijd": datetime.now().strftime("%Y-%m-%d")
                },
                "paginaNummer": 1,
                "paginaGrootte": 100,
                "filterMode": "AlleenNieuwVandaag"
            }
            response = self.session.post(API_DISCOVERY_URL, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
            initial_listings = data.get('d', {}).get('resultaten', [])

            if not initial_listings:
                self._log("API returned no new listings.")
                return []
            self._log(f"Found {len(initial_listings)} IDs. Fetching details concurrently...")
        except Exception as e:
            # NEW: Report API discovery errors
            self._report_error(e, "during API listing discovery")
            return []

        processed_listings = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_id = {executor.submit(self.get_listing_details, r['FrontendAdvertentieId']): r['FrontendAdvertentieId'] for r in initial_listings if r.get('FrontendAdvertentieId')}
            for future in as_completed(future_to_id):
                try: # NEW: Add try/except block here too
                    item = future.result()
                    if not item: continue
                    now = datetime.now()
                    publ_start_dt = self._parse_publ_date(item.get('publstart'))
                    is_live = publ_start_dt and now >= publ_start_dt
                    is_in_selectable_window = now.hour >= APPLICATION_HOUR - 2 # Use config value
                    
                    if is_live:
                        status_text = "LIVE"
                    else:
                        start_time_str = publ_start_dt.strftime('%H:%M') if publ_start_dt else f"{APPLICATION_HOUR}:00"
                        status_text = f"SELECTABLE ({start_time_str})" if is_in_selectable_window else f"PREVIEW ({start_time_str})"
                    
                    can_be_selected = is_live or is_in_selectable_window
                    main_photo = next((m for m in item.get('media', []) if m.get('type') == 'StraatFoto'), None)
                    image_url = f"https:{main_photo['fotoviewer']}" if main_photo and main_photo.get('fotoviewer') else None
                    
                    processed_listings.append({
                        'id': item.get('id'),
                        'address': f"{item.get('straat', '')} {item.get('huisnummer', '')}",
                        'type': item.get('objecttype', 'N/A'),
                        'price_str': f"€ {item.get('kalehuur', '0,00')}",
                        'price_float': self._parse_price(item.get('kalehuur', '')),
                        'status_text': status_text,
                        'is_selectable': can_be_selected,
                        'image_url': image_url
                    })
                except Exception as e:
                    listing_id = future_to_id[future]
                    self._report_error(e, f"processing details for listing ID {listing_id}")

        self._log(f"Processed {len(processed_listings)} listings.")
        return sorted(processed_listings, key=lambda x: x['price_float'])

    def get_listing_details(self, listing_id: str) -> Dict | None:
        """ Fetches detailed information for a single listing via API. """
        payload = {"Id": listing_id, "VolgendeId": 0, "Filters": "gebruik!=Complex|nieuwab==True", "inschrijfnummerTekst": "", "Volgorde": "", "hash": ""}
        try:
            response = self.session.post(API_DETAILS_SINGLE_URL, json=payload, timeout=10)
            response.raise_for_status()
            return response.json().get('d', {}).get('Aanbod')
        except requests.RequestException:
            # This can happen often if an ID is invalid, so don't spam reports
            self._log(f"Could not get details for listing {listing_id}", "warning")
            return None