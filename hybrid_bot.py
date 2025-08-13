# hybrid_bot.py
import json, os, sys, threading, io, logging, webbrowser
from logging.handlers import RotatingFileHandler # NEW
from queue import Queue
import keyring
import requests
from typing import Dict, Any
from datetime import datetime

import ttkbootstrap as ttk
from ttkbootstrap.constants import (DANGER, DISABLED, EW, NORMAL, NSEW, SUCCESS,
                                  WARNING, W, X)
from ttkbootstrap.scrolled import ScrolledFrame
from tkinter import messagebox, PhotoImage
from PIL import Image, ImageTk, ImageDraw

from bot import WoonnetBot
# MODIFIED: Import more from config
from config import BASE_URL, PRE_SELECTION_HOUR, PRE_SELECTION_MINUTE, FINAL_REFRESH_HOUR, FINAL_REFRESH_MINUTE
from reporting import send_discord_report # NEW

# --- Setup ---
APP_NAME = "WoonnetBot"
APP_VERSION = "3.3" # NEW: Versioning in the title
logger = logging.getLogger(APP_NAME)
LOG_FILE = "" # NEW: Global var for log file path

def get_app_dir():
    appdata = os.environ.get('APPDATA')
    if not appdata: return os.path.join(os.path.expanduser("~"), f".{APP_NAME}")
    return os.path.join(appdata, APP_NAME)
APP_DIR = get_app_dir()
PREFS_FILE = os.path.join(APP_DIR, 'user_prefs.json')
SERVICE_ID = f"python:{APP_NAME}"

# MODIFIED: Set up both stream and file logging
def setup_logging():
    global LOG_FILE
    os.makedirs(APP_DIR, exist_ok=True)
    LOG_FILE = os.path.join(APP_DIR, 'bot.log')
    
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Avoid adding handlers multiple times
    if not logger.handlers:
        # Console handler
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        # File handler (rotates after 1MB, keeps 3 backups)
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1*1024*1024, backupCount=3, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

setup_logging()

# ... (The `crop_to_square` and `create_placeholder_image` functions are unchanged) ...
def crop_to_square(image: Image.Image) -> Image.Image:
    w, h = image.size; short = min(w, h); l, t = (w - short) / 2, (h - short) / 2
    return image.crop((l, t, l + short, t + short))

def create_placeholder_image(size=(120, 120)):
    img = Image.new('RGB', size, color='#cccccc'); draw = ImageDraw.Draw(img)
    try: text = "No Image"; tb = draw.textbbox((0, 0), text); draw.text(((size[0]-(tb[2]-tb[0]))/2, (size[1]-(tb[3]-tb[1]))/2), text, fill='#808080')
    except Exception: draw.text((10, 10), "No Image", fill='black')
    return ImageTk.PhotoImage(img)

# ... (ListingWidget is unchanged) ...
class ListingWidget(ttk.Frame):
    def __init__(self, parent, data: Dict[str, Any], session: requests.Session, placeholder_img, **kwargs):
        super().__init__(parent, borderwidth=1, relief="solid", **kwargs)
        self.grid_columnconfigure(2, weight=1)
        self.data = data; self.session = session; self.selected = ttk.BooleanVar()
        is_selectable = data.get('is_selectable', False)
        status_text = data.get('status_text', 'N/A')
        self.image = self._load_image(data.get('image_url'), (120, 120)) or placeholder_img
        self.image_label = ttk.Label(self, image=self.image, cursor="hand2")
        self.image_label.grid(row=0, column=1, rowspan=2, sticky='nsew', padx=(0, 15))
        detail_url = f"{BASE_URL}/detail/{self.data['id']}"
        self.image_label.bind("<Button-1>", lambda e: webbrowser.open_new_tab(detail_url))
        cb = ttk.Checkbutton(self, variable=self.selected, state=NORMAL if is_selectable else DISABLED)
        cb.grid(row=0, column=0, rowspan=2, padx=10, sticky='ns')
        info_frame = ttk.Frame(self); info_frame.grid(row=0, column=2, rowspan=2, sticky=NSEW, pady=5)
        if "LIVE" in status_text: status_style = SUCCESS
        elif "SELECTABLE" in status_text: status_style = WARNING
        else: status_style = 'secondary'
        ttk.Label(info_frame, text=status_text, bootstyle=status_style).pack(anchor='ne') # type: ignore
        ttk.Label(info_frame, text=data.get('address', 'N/A'), font="-weight bold").pack(anchor=W)
        ttk.Label(info_frame, text=f"{data.get('type', 'N/A')} | {data.get('price_str', 'N/A')}").pack(anchor=W)

    def _load_image(self, url, size):
        if not url: return None
        try:
            response = self.session.get(url, stream=True, timeout=5); response.raise_for_status()
            img = crop_to_square(Image.open(io.BytesIO(response.content)))
            img.thumbnail(size, Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception: return None

class App(ttk.Window):
    def __init__(self):
        # MODIFIED: Add version to title
        super().__init__(themename="litera", title=f"Woonnet Bot v{APP_VERSION}", size=(600, 750), minsize=(550, 400))
        self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(1, weight=1)
        
        self.status_queue = Queue(); self.placeholder_img = create_placeholder_image()
        self.listing_widgets: list[ListingWidget] = []
        self.api_session: requests.Session | None = None
        
        # MODIFIED: Pass the log file path to the bot instance
        self.bot_instance = WoonnetBot(self.status_queue, logger, LOG_FILE)
        threading.Thread(target=self.bot_instance.start_headless_browser, daemon=True).start()

        self.create_widgets(); self.load_preferences(); self.set_controls_state('initial')
        self.after(100, self.process_status_queue)
        self.after(60000, self.scheduled_refresh_check)

    # ... (create_widgets is unchanged) ...
    def create_widgets(self):
        main_frame = ttk.Frame(self, padding=10); main_frame.grid(row=0, column=0, sticky=NSEW)
        main_frame.grid_columnconfigure(0, weight=1)
        
        cred_frame = ttk.Labelframe(main_frame, text="Credentials", padding=15); cred_frame.pack(fill=X, pady=(0, 10))
        cred_frame.grid_columnconfigure(1, weight=1)
        ttk.Label(cred_frame, text="Username:").grid(row=0, column=0, sticky=W, padx=5, pady=2)
        self.user_entry = ttk.Entry(cred_frame); self.user_entry.grid(row=0, column=1, sticky=EW, padx=5, pady=2)
        ttk.Label(cred_frame, text="Password:").grid(row=1, column=0, sticky=W, padx=5, pady=2)
        self.pass_entry = ttk.Entry(cred_frame, show="*"); self.pass_entry.grid(row=1, column=1, sticky=EW, padx=5, pady=2)
        self.remember_check = ttk.Checkbutton(cred_frame, text="Remember Me"); self.remember_check.grid(row=2, column=1, sticky=W, padx=5, pady=5)

        control_frame = ttk.Labelframe(main_frame, text="Controls", padding=15); control_frame.pack(fill=X, pady=(0, 10))
        control_frame.grid_columnconfigure((0, 1), weight=1)
        self.login_button = ttk.Button(control_frame, text="Login", command=self.start_login)
        self.login_button.grid(row=0, column=0, columnspan=2, sticky=EW, pady=(0, 10))
        self.discover_button = ttk.Button(control_frame, text="Discover Listings", command=self.start_discovery)
        self.discover_button.grid(row=1, column=0, sticky=EW, padx=(0, 5))
        self.apply_button = ttk.Button(control_frame, text="Apply to Selected (at 8 PM)", command=self.start_apply, bootstyle=DANGER) # type: ignore
        self.apply_button.grid(row=1, column=1, sticky=EW, padx=(5, 0))

        listings_frame = ttk.Labelframe(self, text="Discovered Listings", padding=5); listings_frame.grid(row=1, column=0, sticky=NSEW, padx=10, pady=0)
        listings_frame.grid_rowconfigure(0, weight=1); listings_frame.grid_columnconfigure(0, weight=1)
        self.scrolled_frame = ScrolledFrame(listings_frame, autohide=True); self.scrolled_frame.grid(row=0, column=0, sticky=NSEW)
        
        status_frame = ttk.Labelframe(self, text="Status Log", padding=(10, 5)); status_frame.grid(row=2, column=0, sticky=EW, padx=10, pady=(0, 10))
        status_frame.grid_columnconfigure(0, weight=1)
        self.status_label = ttk.Label(status_frame, text="Waiting for browser to start...", anchor=W); self.status_label.grid(row=0, column=0, sticky=EW)

    # ... (set_controls_state, start_login, run_login_wrapper, start_discovery, run_discovery_wrapper, start_apply, run_apply_wrapper are mostly unchanged) ...
    def set_controls_state(self, state: str):
        if state == 'initial':
            self.login_button.config(state=NORMAL); self.discover_button.config(state=DISABLED); self.apply_button.config(state=DISABLED)
        elif state == 'processing':
            self.login_button.config(state=DISABLED); self.discover_button.config(state=DISABLED); self.apply_button.config(state=DISABLED)
        elif state == 'logged_in':
            self.login_button.config(state=DISABLED); self.discover_button.config(state=NORMAL)
            any_selectable = any(w.data.get('is_selectable', False) for w in self.listing_widgets)
            self.apply_button.config(state=NORMAL if any_selectable else DISABLED)

    def start_login(self):
        username = self.user_entry.get(); password = self.pass_entry.get()
        if not username or not password: messagebox.showerror("Input Error", "Username/Password required."); return
        self.set_controls_state('processing')
        threading.Thread(target=self.run_login_wrapper, args=(username, password), daemon=True).start()

    def run_login_wrapper(self, username, password):
        login_success, session = self.bot_instance.login(username, password)
        if login_success:
            self.api_session = session
            if self.remember_check.instate(['selected']): self.save_credentials(username, password)
        self.after(0, self.set_controls_state, 'logged_in' if login_success else 'initial')
        if login_success:
            self.after(100, self.start_discovery)

    def start_discovery(self):
        self.set_controls_state('processing')
        threading.Thread(target=self.run_discovery_wrapper, daemon=True).start()

    def run_discovery_wrapper(self):
        listings = self.bot_instance.discover_listings_api()
        self.status_queue.put(listings) # Use put, not put_nowait from a thread
        self.after(0, self.set_controls_state, 'logged_in')

    def start_apply(self):
        selected_ids = [w.data['id'] for w in self.listing_widgets if w.selected.get()]
        if not selected_ids: messagebox.showwarning("No Selection", "Please select a listing to apply for."); return
        self.set_controls_state('processing')
        threading.Thread(target=self.run_apply_wrapper, args=(selected_ids,), daemon=True).start()
        
    def run_apply_wrapper(self, ids_to_apply: list[str]):
        self.bot_instance.apply_to_listings(ids_to_apply)
        self.after(0, self.set_controls_state, 'logged_in')

    # MODIFIED: Use config values for scheduled refresh
    def scheduled_refresh_check(self):
        now = datetime.now()
        if self.bot_instance and self.bot_instance.is_logged_in:
            is_pre_selection_time = (now.hour == PRE_SELECTION_HOUR and now.minute == PRE_SELECTION_MINUTE)
            is_final_refresh_time = (now.hour == FINAL_REFRESH_HOUR and now.minute == FINAL_REFRESH_MINUTE)
            if is_pre_selection_time or is_final_refresh_time:
                logger.info(f"--- TRIGGERING SCHEDULED REFRESH AT {now.time():%H:%M} ---")
                self.start_discovery()
        self.after(60000, self.scheduled_refresh_check)

    # ... (process_status_queue, populate_listings, load_preferences, save_credentials, on_closing are mostly unchanged) ...
    def process_status_queue(self):
        while not self.status_queue.empty():
            message = self.status_queue.get_nowait()
            if isinstance(message, list):
                self.populate_listings(message)
            else:
                self.status_label.config(text=str(message))
        self.after(100, self.process_status_queue)
        
    def populate_listings(self, listings: list):
        selected_before = {w.data['id'] for w in self.listing_widgets if w.selected.get()}
        for widget in self.scrolled_frame.winfo_children(): widget.destroy()
        self.listing_widgets.clear()
        if not self.api_session: messagebox.showerror("Error", "API Session not ready."); return
        if not listings: ttk.Label(self.scrolled_frame, text="No new listings found.").pack(pady=20)
        else:
            for data in listings:
                widget = ListingWidget(self.scrolled_frame, data, self.api_session, self.placeholder_img)
                if data['id'] in selected_before: widget.selected.set(True)
                widget.pack(fill=X, pady=5, padx=5)
                self.listing_widgets.append(widget)
        self.set_controls_state('logged_in')

    def load_preferences(self):
        try:
            stored_user = keyring.get_password(SERVICE_ID, "username")
            if stored_user:
                self.user_entry.insert(0, stored_user)
                stored_pass = keyring.get_password(SERVICE_ID, stored_user)
                if stored_pass: self.pass_entry.insert(0, stored_pass); self.remember_check.invoke()
        except Exception: pass

    def save_credentials(self, username, password):
        try: keyring.set_password(SERVICE_ID, "username", username); keyring.set_password(SERVICE_ID, username, password)
        except Exception: pass
    
    def on_closing(self):
        if self.bot_instance: self.bot_instance.quit()
        self.destroy()


if __name__ == "__main__":
    try:
        app = App()
        app.protocol("WM_DELETE_WINDOW", app.on_closing)
        app.mainloop()
    except Exception as e:
        # NEW: Global exception handler to catch anything that slips through
        logger.critical(f"A fatal, unhandled error occurred in the main application loop: {e}", exc_info=True)
        send_discord_report(e, "in the main application __main__ block", LOG_FILE)
        messagebox.showerror("Fatal Error", f"A critical error occurred: {e}\n\nA report has been sent. Please check bot.log for details.")