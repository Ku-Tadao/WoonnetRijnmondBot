"""UI Layer (refactored from legacy hybrid_bot.py).

Contains ListingCard and App classes. Uses core.automation.WoonnetClient.
"""

import os, sys, threading, io, logging, webbrowser
from logging.handlers import RotatingFileHandler
from queue import Queue
from datetime import datetime
from typing import Dict, Any

import tkinter as tk
import ttkbootstrap as ttk
from ttkbootstrap.constants import (DANGER, DISABLED, EW, NORMAL, NSEW, SUCCESS, WARNING, W, X, BOTH)
from ttkbootstrap.scrolled import ScrolledFrame
from tkinter import messagebox
from PIL import Image, ImageTk, ImageDraw
import requests
import keyring

from core.automation import WoonnetClient
from config import (
	BASE_URL, PRE_SELECTION_HOUR, PRE_SELECTION_MINUTE, FINAL_REFRESH_HOUR,
	FINAL_REFRESH_MINUTE, APPLICATION_HOUR
)
from reporting import send_discord_report

APP_NAME = "WoonnetBot"
APP_VERSION = "3.3"
logger = logging.getLogger(APP_NAME)
LOG_FILE = ""

def _init_logging():
	global LOG_FILE
	appdata = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), f'.{APP_NAME}')
	appdir = os.path.join(appdata, APP_NAME)
	os.makedirs(appdir, exist_ok=True)
	LOG_FILE = os.path.join(appdir, 'bot.log')
	logger.setLevel(logging.INFO)
	fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
	if not logger.handlers:
		sh = logging.StreamHandler(sys.stdout); sh.setFormatter(fmt); logger.addHandler(sh)
		fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding='utf-8'); fh.setFormatter(fmt); logger.addHandler(fh)
	return appdir

APP_DIR = _init_logging()
SERVICE_ID = f"python:{APP_NAME}"

def crop_to_square(image: Image.Image) -> Image.Image:
	w, h = image.size; s = min(w, h); l = (w - s)//2; t = (h - s)//2
	return image.crop((l, t, l + s, t + s))

def placeholder_photo(size=(110,110)):
	img = Image.new('RGB', size, '#d0d4d9'); d = ImageDraw.Draw(img)
	text = 'No Image'
	d.text((10, size[1]//2-7), text, fill='#555')
	return ImageTk.PhotoImage(img)

class ListingCard(ttk.Frame):
	def __init__(self, parent, data: Dict[str, Any], session: requests.Session, ph_img):
		super().__init__(parent, padding=6, borderwidth=1, relief='ridge')
		self.data = data; self.session = session
		self.var = tk.BooleanVar()
		self.columnconfigure(3, weight=1)
		is_sel = data.get('is_selectable', False)
		status = data.get('status_text','N/A')
		ttk.Checkbutton(self, variable=self.var, state=NORMAL if is_sel else DISABLED).grid(row=0, column=0, rowspan=2, sticky='ns', padx=(0,6))
		self.image = self._load_img(data.get('image_url')) or ph_img
		lbl = ttk.Label(self, image=self.image, cursor='hand2'); lbl.grid(row=0, column=1, rowspan=2, padx=(0,10))
		detail_url = f"{BASE_URL}/detail/{data['id']}"; lbl.bind('<Button-1>', lambda e: webbrowser.open_new_tab(detail_url))
		if 'LIVE' in status: style = SUCCESS
		elif 'SELECTABLE' in status: style = WARNING
		else: style = 'secondary'
		ttk.Label(self, text=status, bootstyle=f'{style}-inverse').grid(row=0, column=2, sticky='w') # type: ignore
		ttk.Label(self, text=data.get('address','N/A'), font=('Segoe UI', 10, 'bold')).grid(row=1, column=2, sticky='w')
		ttk.Label(self, text=f"{data.get('type','')} | {data.get('price_str','')}").grid(row=1, column=3, sticky='w')

	def _load_img(self, url):
		if not url: return None
		try:
			r = self.session.get(url, timeout=5); r.raise_for_status()
			img = crop_to_square(Image.open(io.BytesIO(r.content)))
			img.thumbnail((110,110), Image.Resampling.LANCZOS)
			return ImageTk.PhotoImage(img)
		except Exception: return None

class App(ttk.Window):
	def __init__(self):
		super().__init__(themename='litera', title=f'Woonnet Bot v{APP_VERSION}', size=(720,820), minsize=(640,540))
		self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(2, weight=1)
		self.status_queue = Queue(); self.cards = []; self.api_session = None
		self.selection_var = tk.StringVar(value='No listings loaded')
		self.placeholder = placeholder_photo()
		self.bot = WoonnetClient(self.status_queue, logger, LOG_FILE)
		try: self.bot.set_cache_path(APP_DIR); self.bot.load_cached_ids()
		except Exception: pass
		threading.Thread(target=self.bot.start_headless_browser, daemon=True).start()
		self._build_menu(); self._build_header(); self._build_body(); self.load_credentials()
		self.set_controls('initial'); self.after(120, self.pump_status); self.after(60000, self.scheduled_refresh_check)
		self.after(5000, self._auto_refresh_loop)
		self._last_change_timestamp = None

	def _build_menu(self):
		m = tk.Menu(self)
		file_menu = tk.Menu(m, tearoff=0); file_menu.add_command(label='Discover', command=self.start_discovery); file_menu.add_separator(); file_menu.add_command(label='Exit', command=self.on_close)
		m.add_cascade(label='File', menu=file_menu)
		self.debug_var = tk.BooleanVar(value=False)
		view_menu = tk.Menu(m, tearoff=0); view_menu.add_checkbutton(label='Debug Mode', variable=self.debug_var, command=self._toggle_debug)
		m.add_cascade(label='View', menu=view_menu)
		help_menu = tk.Menu(m, tearoff=0); help_menu.add_command(label='Open Logs', command=lambda: os.startfile(APP_DIR)); help_menu.add_command(label='About', command=self._about)
		m.add_cascade(label='Help', menu=help_menu); self.config(menu=m)

	def _build_header(self):
		hdr = ttk.Frame(self, padding=(12,8)); hdr.grid(row=0, column=0, sticky=EW)
		ttk.Label(hdr, text='Woonnet Bot', font=('Segoe UI Semibold',16)).pack(side='left')
		ttk.Label(hdr, text='Automated discovery & on-time applications', bootstyle='secondary').pack(side='left', padx=10) # type: ignore

	def _build_body(self):
		body = ttk.Frame(self, padding=12); body.grid(row=1, column=0, sticky=NSEW); body.grid_columnconfigure(0, weight=1)
		creds = ttk.Labelframe(body, text='Account', padding=10); creds.pack(fill=X, pady=(0,10)); creds.grid_columnconfigure(1, weight=1)
		ttk.Label(creds, text='Username:').grid(row=0,column=0,sticky=W); self.user_entry = ttk.Entry(creds); self.user_entry.grid(row=0,column=1,sticky=EW,padx=4,pady=2)
		ttk.Label(creds, text='Password:').grid(row=1,column=0,sticky=W); self.pass_entry = ttk.Entry(creds, show='*'); self.pass_entry.grid(row=1,column=1,sticky=EW,padx=4,pady=2)
		self.remember_chk = ttk.Checkbutton(creds, text='Remember credentials'); self.remember_chk.grid(row=2,column=1,sticky=W,pady=4)
		acts = ttk.Labelframe(body, text='Actions', padding=10); acts.pack(fill=X, pady=(0,10)); acts.grid_columnconfigure((0,1,2), weight=1)
		self.login_btn = ttk.Button(acts, text='Login', command=self.start_login, bootstyle=SUCCESS); self.login_btn.grid(row=0,column=0,sticky=EW,padx=(0,6)) # type: ignore
		self.discover_btn = ttk.Button(acts, text='Discover Listings', command=self.start_discovery); self.discover_btn.grid(row=0,column=1,sticky=EW,padx=6)
		self.apply_btn = ttk.Button(acts, text='Queue Applications (20:00)', command=self.start_apply, bootstyle=DANGER); self.apply_btn.grid(row=0,column=2,sticky=EW,padx=(6,0)) # type: ignore
		bar = ttk.Frame(body); bar.pack(fill=X, pady=(0,4))
		self.sel_label = ttk.Label(bar, textvariable=self.selection_var, anchor=W); self.sel_label.pack(side='left')
		ttk.Button(bar, text='Select All', command=self._select_all, bootstyle='secondary-outline').pack(side='right', padx=2) # type: ignore
		ttk.Button(bar, text='Clear', command=self._clear_selection, bootstyle='secondary-outline').pack(side='right', padx=2) # type: ignore
		lst_frame = ttk.Labelframe(body, text='Listings', padding=4); lst_frame.pack(fill=BOTH, expand=True); lst_frame.grid_columnconfigure(0, weight=1); lst_frame.grid_rowconfigure(0, weight=1)
		self.scroller = ScrolledFrame(lst_frame, autohide=True); self.scroller.grid(row=0, column=0, sticky=NSEW)
		stat = ttk.Labelframe(self, text='Status & Log', padding=(10,6)); stat.grid(row=3, column=0, sticky=NSEW, padx=12, pady=(0,12)); stat.grid_columnconfigure(0, weight=1); stat.grid_rowconfigure(1, weight=1)
		self.status_label = ttk.Label(stat, text='Starting browser...', anchor=W); self.status_label.grid(row=0,column=0,sticky=EW)
		self.log_text = tk.Text(stat, height=7, wrap='word', state='disabled', font=('Consolas',9)); self.log_text.grid(row=1,column=0,sticky=NSEW,pady=(6,4))
		for tag,color in {'INFO':'#1f6feb','ERROR':'#d73a49','WARNING':'#d29922','SUCCESS':'#2da44e'}.items(): self.log_text.tag_configure(tag, foreground=color)
		self.progress = ttk.Progressbar(stat, mode='indeterminate'); self.progress.grid(row=2,column=0,sticky=EW)

	def set_controls(self, state: str):
		if state == 'processing':
			try: self.progress.start(14)
			except Exception: pass
			self.login_btn.config(state=DISABLED); self.discover_btn.config(state=DISABLED); self.apply_btn.config(state=DISABLED)
		elif state == 'initial':
			self.progress.stop(); self.login_btn.config(state=NORMAL); self.discover_btn.config(state=DISABLED); self.apply_btn.config(state=DISABLED)
		elif state == 'logged_in':
			self.progress.stop(); self.login_btn.config(state=DISABLED); self.discover_btn.config(state=NORMAL); any_sel = any(c.data.get('is_selectable') for c in self.cards); self.apply_btn.config(state=NORMAL if any_sel else DISABLED)
		self._update_selection_label()

	def start_login(self):
		u = self.user_entry.get().strip(); p = self.pass_entry.get().strip()
		if not u or not p: messagebox.showerror('Input Error','Username/Password required.'); return
		self.set_controls('processing'); threading.Thread(target=self._login_worker, args=(u,p), daemon=True).start()

	def _login_worker(self, u, p):
		ok, session = self.bot.login(u,p)
		if ok:
			self.api_session = session
			if self.remember_chk.instate(['selected']): self.save_credentials(u,p)
		self.after(0, lambda: self.set_controls('logged_in' if ok else 'initial'))
		if ok: self.after(200, self.start_discovery)

	def start_discovery(self):
		if not self.api_session: return
		self.set_controls('processing'); threading.Thread(target=self._discover_worker, daemon=True).start()

	def _discover_worker(self):
		listings = self.bot.discover_listings_api(); self.status_queue.put(listings); self.after(0, lambda: self.set_controls('logged_in'))
		if listings: self._new_listings_notify(len(listings))

	def start_apply(self):
		ids = [c.data['id'] for c in self.cards if c.var.get()]
		if not ids: messagebox.showwarning('No Selection','Select at least one listing.'); return
		self.set_controls('processing'); threading.Thread(target=self._apply_worker, args=(ids,), daemon=True).start()

	def _apply_worker(self, ids):
		self.bot.apply_to_listings(ids); self.after(0, lambda: self.set_controls('logged_in'))

	def scheduled_refresh_check(self):
		now = datetime.now()
		if self.bot and self.bot.is_logged_in:
			if (now.hour == PRE_SELECTION_HOUR and now.minute == PRE_SELECTION_MINUTE) or (now.hour == FINAL_REFRESH_HOUR and now.minute == FINAL_REFRESH_MINUTE):
				logger.info('Scheduled refresh triggered.'); self.start_discovery()
		self.after(60000, self.scheduled_refresh_check)

	def pump_status(self):
		while not self.status_queue.empty():
			msg = self.status_queue.get_nowait()
			if isinstance(msg, list): self._populate_listings(msg)
			else: self.status_label.config(text=str(msg)); self._append_log(str(msg))
		self.after(150, self.pump_status)

	def _populate_listings(self, listings):
		prev_selected = {c.data['id'] for c in self.cards if c.var.get()}
		for child in self.scroller.winfo_children(): child.destroy()
		self.cards.clear()
		if not listings:
			ttk.Label(self.scroller, text='No new listings found.', font=('Segoe UI',11)).pack(pady=24)
		else:
			for data in listings:
				if self.api_session is None: continue
				card = ListingCard(self.scroller, data, self.api_session, self.placeholder)
				if data['id'] in prev_selected: card.var.set(True)
				card.pack(fill=X, pady=5, padx=5); self.cards.append(card)
		self.set_controls('logged_in')

	def _select_all(self):
		for c in self.cards:
			if c.data.get('is_selectable'): c.var.set(True)
		self._update_selection_label()

	def _clear_selection(self):
		for c in self.cards: c.var.set(False)
		self._update_selection_label()

	def _update_selection_label(self):
		total = len(self.cards); selected = sum(1 for c in self.cards if c.var.get()); selectable = sum(1 for c in self.cards if c.data.get('is_selectable'))
		self.selection_var.set('No listings loaded' if total==0 else f'Selected {selected}/{selectable} (Total {total})')

	def _append_log(self, text: str):
		level = 'INFO'; u = text.upper()
		if 'ERROR' in u: level='ERROR'
		elif 'WARNING' in u: level='WARNING'
		elif 'SUCCESS' in u: level='SUCCESS'
		self.log_text.configure(state='normal'); self.log_text.insert('end', f"{datetime.now():%H:%M:%S} | {text}\n", level); self.log_text.see('end')
		if int(float(self.log_text.index('end-1c').split('.')[0])) > 800: self.log_text.delete('1.0', '150.0')
		self.log_text.configure(state='disabled')

	def _toggle_debug(self):
		self.bot.set_debug(bool(self.debug_var.get()))

	def _auto_refresh_loop(self):
		now = datetime.now(); minutes_to_target = max(0, APPLICATION_HOUR * 60 - (now.hour * 60 + now.minute))
		if not self.bot.is_logged_in: interval = 30_000
		else:
			if minutes_to_target > 120: interval = 5 * 60 * 1000
			elif minutes_to_target > 60: interval = 90 * 1000
			elif minutes_to_target > 15: interval = 30 * 1000
			elif minutes_to_target > 0: interval = 15 * 1000
			else: interval = 120 * 1000
			if self.bot.is_logged_in and (minutes_to_target <= 15 or not self.cards or self.debug_var.get()): self.start_discovery()
		if self.debug_var.get(): self.status_label.config(text=f"Next auto refresh in {int(interval/1000)}s (t-minus {minutes_to_target}m)")
		self.after(interval, self._auto_refresh_loop)

	def _new_listings_notify(self, count: int):
		try: self.bell()
		except Exception: pass
		self._append_log(f"SUCCESS: {count} new/changed listings.")

	def load_credentials(self):
		try:
			u = keyring.get_password(SERVICE_ID, 'username')
			if u:
				self.user_entry.insert(0, u)
				p = keyring.get_password(SERVICE_ID, u)
				if p:
					self.pass_entry.insert(0, p); self.remember_chk.invoke()
		except Exception: pass

	def save_credentials(self, u, p):
		try: keyring.set_password(SERVICE_ID, 'username', u); keyring.set_password(SERVICE_ID, u, p)
		except Exception: pass

	def _about(self):
		messagebox.showinfo('About', f'WoonnetBot v{APP_VERSION}\nImproved UI build.')

	def on_close(self):
		try: self.bot.quit()
		except Exception: pass
		self.destroy()

	def on_closing(self): self.on_close()
	def set_controls_state(self, state): self.set_controls(state)
	def create_widgets(self): pass


def run():  # simple entrypoint convenience
	app = App(); app.protocol("WM_DELETE_WINDOW", app.on_closing); app.mainloop()


if __name__ == "__main__":
	try:
		run()
	except Exception as e:
		logger.critical(f"Fatal error in UI main loop: {e}", exc_info=True)
		send_discord_report(e, "in ui.app main", LOG_FILE)
		messagebox.showerror("Fatal Error", f"A critical error occurred: {e}\n\nA report has been sent. Please check bot.log for details.")

