from __future__ import annotations
import sys, os
from typing import List, Dict, Any
import threading
import requests
import keyring
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QScrollArea, QFrame, QStatusBar, QMessageBox, QMenuBar, QMenu, QCheckBox,
    QGraphicsDropShadowEffect, QDialog, QDialogButtonBox
)
from PySide6.QtCore import Qt, QThread, Slot, Signal, QTimer, QObject
from PySide6.QtGui import QColor, QPixmap, QImage, QMouseEvent, QIcon

from core.automation import WoonnetClient
from ui_qt.worker import BotWorker
from ui_qt.flow_layout import FlowLayout

APP_TITLE = "Woonnet Bot (Qt Preview)"
APP_NAME = "WoonnetBot"
SERVICE_ID = f"python:{APP_NAME}"

# Logger setup (only once)
logger = logging.getLogger("WoonnetQt")
if not logger.handlers:
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    try:
        fh = logging.FileHandler('qt_ui.log', encoding='utf-8')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)


_IMAGE_EXECUTOR = ThreadPoolExecutor(max_workers=4)
# Gallery cache now stores QImage objects (thread-safe to create) to avoid creating QPixmap off the GUI thread
_GALLERY_CACHE: 'OrderedDict[str, QImage]' = OrderedDict()
_GALLERY_CACHE_LIMIT = 200

class ListingCard(QFrame):
    imageLoaded = Signal(QPixmap)
    imageError = Signal()

    _image_cache: 'OrderedDict[str, QPixmap]' = OrderedDict()
    _image_cache_limit = 100

    def __init__(self, data: Dict[str, Any], parent=None, session: requests.Session | None = None):
        super().__init__(parent)
        self.setObjectName("CardFrame")
        self.data = data
        self._session = session  # reuse authenticated session (cookies, headers)
        self._img_label: QLabel | None = None
        self._shadow: QGraphicsDropShadowEffect | None = None

        # Layout root
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 14, 14, 14)
        row.setSpacing(16)

        # Image placeholder
        img_lbl = QLabel("IMG")
        img_lbl.setObjectName("cardImage")
        img_lbl.setAlignment(Qt.AlignCenter)
        img_lbl.setFixedSize(160, 120)
        row.addWidget(img_lbl)
        self._img_label = img_lbl
        self.imageLoaded.connect(self._apply_pixmap)
        self.imageError.connect(self._mark_image_error)
        img_lbl.mousePressEvent = self._on_image_clicked  # type: ignore

        # Middle content
        mid = QVBoxLayout()
        mid.setSpacing(6)
        status_text = data.get('status_text', 'N/A')
        badge = QLabel(status_text)
        st_upper = status_text.upper()
        if 'LIVE' in st_upper:
            badge.setObjectName('statusBadgeLive')
        elif 'SELECTABLE' in st_upper:
            badge.setObjectName('statusBadgeSelectable')
        else:
            badge.setObjectName('statusBadgePreview')
        raw_addr = data.get('address', '').strip()
        addr = QLabel(); addr.setObjectName('addressLabel'); addr.setTextFormat(Qt.RichText); addr.setWordWrap(True); addr.setText(f"<b>{raw_addr}</b>")
        # Build meta line dynamically, omit unknown rooms instead of showing '? rooms'
        meta_parts = []
        listing_type = data.get('type', 'N/A')
        if listing_type:
            meta_parts.append(listing_type)
        price_str = data.get('price_str') or ''
        if price_str.strip():
            meta_parts.append(price_str.strip())
        rooms_val = data.get('rooms')
        if rooms_val is not None:
            rv_str = str(rooms_val).strip()
            if rv_str and rv_str != '?' and rv_str.lower() != 'none':
                try:
                    n_rooms = int(float(rv_str))
                    label_rooms = f"{n_rooms} room{'s' if n_rooms != 1 else ''}"
                except Exception:
                    label_rooms = f"{rv_str} rooms"
                meta_parts.append(f"<span class='dim'>{label_rooms}</span>")
        meta = QLabel('  ·  '.join(meta_parts))
        meta.setObjectName('metaLabel'); meta.setTextFormat(Qt.RichText); meta.setWordWrap(True)
        mid.addWidget(badge); mid.addWidget(addr); mid.addWidget(meta)
        row.addLayout(mid, 1)

        # Right side actions (placeholder)
        right = QVBoxLayout(); right.setSpacing(6)
        apply_btn = QPushButton("Apply"); apply_btn.setEnabled(False)
        right.addWidget(apply_btn); right.addStretch(1)
        row.addLayout(right)

        # Fixed width for consistent wrapping
        self.setFixedWidth(440)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setColor(QColor(0, 0, 0, 170))
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 4)
        self.setGraphicsEffect(shadow)
        self._shadow = shadow

        # Async image load (thumbnail) + prepare all images list
        img_url = data.get('image_url')
        raw_list = data.get('image_urls') or []
        normalized: List[str] = []
        for u in raw_list:
            if not u:
                continue
            if u.startswith('//'):
                u = 'https:' + u
            normalized.append(u)
        if not normalized and img_url:
            thumb_single = img_url
            if thumb_single.startswith('//'):
                thumb_single = 'https:' + thumb_single
            normalized.append(thumb_single)
        self._all_images = normalized
        logger.debug(f"[CARD] images count={len(self._all_images)} first={self._all_images[0] if self._all_images else None}")
        # choose thumbnail (prefer explicit image_url else first normalized)
        thumb_url = None
        if img_url:
            thumb_url = 'https:' + img_url if img_url.startswith('//') else img_url
        elif self._all_images:
            thumb_url = self._all_images[0]
        if thumb_url:
            logger.debug(f"[CARD] schedule image id={data.get('id')} url={thumb_url}")
            _IMAGE_EXECUTOR.submit(self._load_image, thumb_url)
        else:
            logger.debug(f"[CARD] no image for id={data.get('id')} addr={data.get('address')}")

        # Kick off background prefetch of gallery images (excluding thumbnail) after slight delay
        if len(self._all_images) > 1:
            def _prefetch():
                time.sleep(0.25)  # allow UI to settle
                for u in self._all_images:
                    if u == thumb_url:
                        continue
                    if u in _GALLERY_CACHE:
                        # refresh LRU position
                        try:
                            _GALLERY_CACHE.move_to_end(u)
                        except Exception:
                            pass
                        continue
                    try:
                        getter = self._session.get if self._session else requests.get
                        r = getter(u, timeout=6)
                        if r.status_code == 200 and r.content:
                            qimg = QImage.fromData(r.content)
                            if not qimg.isNull():
                                _GALLERY_CACHE[u] = qimg  # store QImage; convert to QPixmap in GUI thread
                                if len(_GALLERY_CACHE) > _GALLERY_CACHE_LIMIT:
                                    # trim oldest 10% to reduce churn
                                    trim = max(10, int(_GALLERY_CACHE_LIMIT * 0.1))
                                    for _ in range(trim):
                                        try:
                                            _GALLERY_CACHE.popitem(last=False)
                                        except KeyError:
                                            break
                                logger.debug(f"[PREFETCH] cached gallery image {u}")
                        else:
                            logger.debug(f"[PREFETCH] skip status={r.status_code} url={u}")
                    except Exception as e:
                        logger.debug(f"[PREFETCH] error {u}: {e}")
            threading.Thread(target=_prefetch, daemon=True).start()

    def _load_image(self, url: str):
        try:
            if url in self._image_cache:
                pix = self._image_cache[url]
                # LRU move
                self._image_cache.move_to_end(url)
                logger.debug(f"[IMG] cache hit {url}")
                self.imageLoaded.emit(pix)
                return

            attempts = 2
            last_exc: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    getter = self._session.get if self._session else requests.get
                    resp = getter(url, timeout=8)
                    logger.debug(f"[IMG] http {resp.status_code} {url} bytes={len(resp.content)} attempt={attempt}")
                    if resp.status_code >= 500:
                        raise RuntimeError(f"server {resp.status_code}")
                    if resp.status_code != 200 or not resp.content:
                        logger.debug(f"[IMG] non-200/empty {url}")
                        self.imageError.emit()
                        return
                    img = QImage.fromData(resp.content)
                    if img.isNull():
                        logger.debug(f"[IMG] qimage null {url}")
                        self.imageError.emit()
                        return
                    if not self._img_label:
                        return
                    w, h = self._img_label.width(), self._img_label.height()
                    scaled = QPixmap.fromImage(img).scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    x = (scaled.width() - w)//2; y = (scaled.height() - h)//2
                    pix = scaled.copy(x, y, w, h)
                    self._image_cache[url] = pix
                    # enforce LRU capacity
                    if len(self._image_cache) > self._image_cache_limit:
                        evicted_url, _ = self._image_cache.popitem(last=False)
                        logger.debug(f"[IMG] lru evict {evicted_url}")
                    logger.debug(f"[IMG] prepared {url} final={pix.width()}x{pix.height()} target={w}x{h}")
                    self.imageLoaded.emit(pix)
                    return
                except Exception as e:
                    last_exc = e
                    logger.debug(f"[IMG] attempt {attempt} failed {url}: {e}")
                    if attempt < attempts:
                        time.sleep(0.4 * attempt)
                        continue
            logger.exception(f"[IMG] giving up {url}: {last_exc}")
            self.imageError.emit()
        except Exception as outer:
            logger.exception(f"[IMG] unexpected error {url}: {outer}")
            self.imageError.emit()

    @Slot(QPixmap)
    def _apply_pixmap(self, pix: QPixmap):
        if not self._img_label:
            return
        self._img_label.setText("")
        self._img_label.setPixmap(pix)
        logger.debug("[IMG] applied via signal")
        # Robust fade-in via opacity effect
        try:
            from PySide6.QtWidgets import QGraphicsOpacityEffect
            from PySide6.QtCore import QPropertyAnimation
            eff = QGraphicsOpacityEffect(self._img_label)
            self._img_label.setGraphicsEffect(eff)
            anim = QPropertyAnimation(eff, b"opacity", self)
            anim.setDuration(240)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception as e:
            logger.debug(f"[ANIM] fade failed: {e}")

    @Slot()
    def _mark_image_error(self):
        if not self._img_label:
            return
        self._img_label.setObjectName("cardImageError")
        self._img_label.setText("NO IMAGE")
        self._img_label.setStyleSheet("font-weight:600;")
        logger.debug("[IMG] marked error placeholder")

    # Hover elevation
    def enterEvent(self, event):  # type: ignore
        if self._shadow:
            self._shadow.setBlurRadius(30)
            self._shadow.setOffset(0, 6)
        return super().enterEvent(event)

    def leaveEvent(self, event):  # type: ignore
        if self._shadow:
            self._shadow.setBlurRadius(22)
            self._shadow.setOffset(0, 4)
        return super().leaveEvent(event)

    # --- Image gallery dialog ---
    def _on_image_clicked(self, event: QMouseEvent):  # type: ignore
        if not self._all_images:
            logger.debug("[GALLERY] no images list empty")
            return
        logger.debug(f"[GALLERY] open with {len(self._all_images)} images")
        dlg = QDialog(self)
        dlg.setWindowTitle("Images")
        dlg.resize(820, 560)
        v = QVBoxLayout(dlg)
        title = QLabel(self.data.get('address',''))
        title.setAlignment(Qt.AlignCenter)
        v.addWidget(title)
        img_label = QLabel("Loading...")
        img_label.setAlignment(Qt.AlignCenter)
        img_label.setMinimumSize(400, 300)
        v.addWidget(img_label, 1)
        nav = QHBoxLayout()
        prev_btn = QPushButton("◀ Previous")
        next_btn = QPushButton("Next ▶")
        counter_lbl = QLabel("")
        nav.addWidget(prev_btn)
        nav.addStretch(1)
        nav.addWidget(counter_lbl)
        nav.addStretch(1)
        nav.addWidget(next_btn)
        v.addLayout(nav)
        close_buttons = QDialogButtonBox(QDialogButtonBox.Close)
        v.addWidget(close_buttons)
        close_buttons.rejected.connect(dlg.reject)

        state = {'idx': 0, 'raw_cache': {}}  # raw_cache per dialog

        # Bridge object to safely transfer QImage from worker thread to GUI thread
        class _GalleryBridge(QObject):
            deliver = Signal(str, QImage)  # url, image

        bridge = _GalleryBridge(dlg)

        def _on_deliver(u: str, qimg: QImage):
            # Convert to QPixmap on GUI thread only
            from PySide6.QtGui import QPixmap as _QPx
            if qimg.isNull():
                if self._all_images[state['idx']] == u:
                    img_label.setText("Invalid")
                return
            pix = _QPx.fromImage(qimg)
            state['raw_cache'][u] = pix
            # Only update display if still on this image
            if self._all_images[state['idx']] == u:
                scale_and_set(pix)
            logger.debug(f"[GALLERY] applied via bridge url={u} size={pix.width()}x{pix.height()}")

        bridge.deliver.connect(_on_deliver)

        def update_counter():
            counter_lbl.setText(f"{state['idx']+1} / {len(self._all_images)}")

        def scale_and_set(pix_raw: QPixmap):
            target_size = img_label.size()
            if target_size.width() < 20:
                target_size = dlg.size()
            margin = 40
            sw = max(50, target_size.width()-margin)
            sh = max(50, target_size.height()-margin)
            scaled = pix_raw.scaled(sw, sh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            img_label.setPixmap(scaled)
            img_label.setText("")

        def load_index():
            update_counter()
            idx = state['idx']
            url = self._all_images[idx]
            img_label.setText("Loading...")
            # prefer existing raw
            raw = state['raw_cache'].get(url) if isinstance(state['raw_cache'], dict) else None
            if raw:
                scale_and_set(raw); return
            # Check global prefetch cache
            global_img = _GALLERY_CACHE.get(url)
            if global_img:
                # Directly emit through bridge for consistency
                bridge.deliver.emit(url, global_img)
                logger.debug(f"[GALLERY] used prefetched cache url={url}")
                return
            def worker():
                try:
                    logger.debug(f"[GALLERY] fetch start idx={idx} url={url}")
                    getter = self._session.get if getattr(self, '_session', None) else requests.get
                    attempts = 3
                    last_status = None
                    r = None
                    for a in range(1, attempts+1):
                        try:
                            r = getter(url, timeout=10)
                            last_status = r.status_code
                            if r.status_code == 200 and r.content:
                                break
                            logger.debug(f"[GALLERY] attempt {a} status={r.status_code} url={url}")
                        except Exception as ie:
                            logger.debug(f"[GALLERY] attempt {a} error {ie} url={url}")
                        time.sleep(0.35 * a)
                    if not r or r.status_code != 200 or not r.content:
                        logger.debug(f"[GALLERY] fetch fail status={last_status} url={url}")
                        QTimer.singleShot(0, lambda s=last_status: img_label.setText(f"Failed ({s})")); return
                    qimg = QImage.fromData(r.content)
                    if qimg.isNull():
                        logger.debug(f"[GALLERY] invalid image data url={url}")
                        QTimer.singleShot(0, lambda: img_label.setText("Invalid")); return
                    # Emit image to GUI thread for conversion & display
                    bridge.deliver.emit(url, qimg)
                    logger.debug(f"[GALLERY] fetch ok idx={idx} url={url} size={qimg.width()}x{qimg.height()}")
                except Exception as e:
                    logger.debug(f"[GALLERY] fetch error {url}: {e}")
                    QTimer.singleShot(0, lambda: img_label.setText("Error"))
            threading.Thread(target=worker, daemon=True).start()

        def prev_clicked():
            if state['idx'] > 0:
                state['idx'] -= 1; load_index()
            else:  # wrap
                state['idx'] = len(self._all_images) - 1; load_index()

        def next_clicked():
            if state['idx'] < len(self._all_images) - 1:
                state['idx'] += 1; load_index()
            else:  # wrap
                state['idx'] = 0; load_index()

        def key_press(ev):  # simple key handling
            from PySide6.QtGui import QKeyEvent
            if isinstance(ev, QKeyEvent):
                if ev.key() in (Qt.Key_Left, Qt.Key_A):
                    prev_clicked(); ev.accept(); return
                if ev.key() in (Qt.Key_Right, Qt.Key_D):
                    next_clicked(); ev.accept(); return
                if ev.key() in (Qt.Key_Escape,):
                    dlg.reject(); ev.accept(); return
            return orig_event(ev)

        prev_btn.clicked.connect(prev_clicked)
        next_btn.clicked.connect(next_clicked)

        # Resize handling to rescale current image
        orig_resize = dlg.resizeEvent
        def resize_event(e):
            if img_label.pixmap():
                # rescale current raw
                url = self._all_images[state['idx']]
                raw_pix = state['raw_cache'].get(url)
                if raw_pix:
                    scale_and_set(raw_pix)
            return orig_resize(e) if orig_resize else None
        dlg.resizeEvent = resize_event  # type: ignore

        # Keyboard navigation
        orig_event = dlg.event
        dlg.event = key_press  # type: ignore

        QTimer.singleShot(60, load_index)
        dlg.exec()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1024, 780)
        self._discover_in_progress = False

        # Client & worker setup
        import threading
        self.client = WoonnetClient(status_queue=None, logger=__import__('logging').getLogger(APP_TITLE), log_file_path='qt.log')  # type: ignore
        threading.Thread(target=self.client.start_headless_browser, daemon=True).start()
        self.thread = QThread(self)
        self.worker = BotWorker(self.client)
        self.worker.moveToThread(self.thread)
        self.thread.start()

        self._build_menu()
        self._build_ui()
        self._load_credentials()
        self._connect_signals()
        self._load_theme()

    # UI construction -------------------------------------------------
    def _build_menu(self):
        mb: QMenuBar = self.menuBar()
        file_menu: QMenu = mb.addMenu("File")
        file_menu.addAction("Discover", self._trigger_discover)
        file_menu.addSeparator()
        file_menu.addAction("Exit", self.close)
        view_menu: QMenu = mb.addMenu("View")
        self.metrics_visible = True
        view_menu.addAction("Toggle Metrics", self._toggle_metrics)

    def _build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(16)

        # Login / controls row
        bar = QHBoxLayout(); bar.setSpacing(10)
        self.user_edit = QLineEdit(); self.user_edit.setPlaceholderText('Username')
        self.pass_edit = QLineEdit(); self.pass_edit.setPlaceholderText('Password'); self.pass_edit.setEchoMode(QLineEdit.Password)
        self.login_btn = QPushButton('Login'); self.login_btn.setObjectName('PrimaryButton'); self.login_btn.clicked.connect(self._do_login)
        self.discover_btn = QPushButton('Discover'); self.discover_btn.clicked.connect(self._trigger_discover); self.discover_btn.setEnabled(False)
        self.remember_chk = QCheckBox('Remember')
        for w in (self.user_edit, self.pass_edit, self.login_btn, self.discover_btn, self.remember_chk):
            bar.addWidget(w)
        bar.addStretch(1)
        outer.addLayout(bar)

        # Listings area (scroll + flow layout)
        self.scroll_area = QScrollArea(); self.scroll_area.setWidgetResizable(True)
        self.cards_container = QWidget()
        self.cards_layout = FlowLayout(self.cards_container, hspacing=22, vspacing=22)
        self.cards_container.setLayout(self.cards_layout)
        self.scroll_area.setWidget(self.cards_container)
        outer.addWidget(self.scroll_area, 1)

        # Metrics bar
        self.metrics_bar = QLabel(objectName='MetricsBar')
        self.metrics_bar.setText('Metrics: —')
        outer.addWidget(self.metrics_bar)

        # Status bar
        sb = QStatusBar(); self.setStatusBar(sb)
        sb.showMessage('Ready')

    # Support ---------------------------------------------------------
    def _connect_signals(self):
        self.worker.listings.connect(self._on_listings)
        self.worker.metrics.connect(self._on_metrics)
        self.worker.loggedIn.connect(self._on_logged_in)

    def _load_theme(self):
        qss_path = os.path.join(os.path.dirname(__file__), 'theme.qss')
        try:
            with open(qss_path, 'r', encoding='utf-8') as f:
                self.setStyleSheet(f.read())
        except Exception:
            pass

    # Actions ---------------------------------------------------------
    @Slot()
    def _do_login(self):
        u = self.user_edit.text().strip(); p = self.pass_edit.text().strip()
        if not u or not p:
            QMessageBox.warning(self, 'Login', 'Username & password required')
            return
        self.statusBar().showMessage('Logging in...')
        self.login_btn.setEnabled(False)
        import threading
        threading.Thread(target=lambda: self.worker.login(u, p), daemon=True).start()

    @Slot(bool)
    def _on_logged_in(self, ok: bool):
        if ok:
            self.statusBar().showMessage('Logged in')
            self.discover_btn.setEnabled(True)
            if self.remember_chk.isChecked():
                self._save_credentials(self.user_edit.text().strip(), self.pass_edit.text())
            # Auto-trigger first discovery
            logger.debug("[LOGIN] Auto discovery trigger after successful login")
            self._trigger_discover()
        else:
            self.statusBar().showMessage('Login failed')
            self.login_btn.setEnabled(True)

    @Slot()
    def _trigger_discover(self):
        if self._discover_in_progress:
            logger.debug("[DISCOVER] Ignored trigger (already in progress)")
            return
        self._discover_in_progress = True
        self.statusBar().showMessage('Discovering...')
        logger.debug("[DISCOVER] Start discovery thread")
        import threading
        threading.Thread(target=self.worker.discover, daemon=True).start()

    # Slots -----------------------------------------------------------
    @Slot(list)
    def _on_listings(self, listings: List[Dict[str, Any]]):
        logger.debug(f"[DISCOVER] Received {len(listings)} listings")
        # Clear existing cards
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            w = item.widget() if item else None
            if w:
                w.deleteLater()
        for data in listings:
            imgs_ct = len(data.get('image_urls') or [])
            logger.debug(f"[LISTING] id={data.get('id')} addr={data.get('address')} image_url={data.get('image_url')} imgs={imgs_ct}")
            self.cards_layout.addWidget(ListingCard(data, session=self.client.session))
        self.statusBar().showMessage(f"Loaded {len(listings)} listings")
        self._discover_in_progress = False

    @Slot(dict)
    def _on_metrics(self, m: Dict[str, Any]):
        self.metrics_bar.setText(
            f"HTTP {m['http_requests']} ({m['http_errors']} err) avg {m['avg_http_ms']:.0f}ms | "
            f"Disc {m['discovery_runs']} last {m['last_discovery_ms']:.0f}ms +{m['last_change_new']}/-{m['last_change_disappeared']} | "
            f"Listings {m['last_listings_processed']} detail {m['avg_detail_fetch_ms']:.0f}ms | Apply {m['apply_success']}/{m['apply_attempts']}"
        )

    def _toggle_metrics(self):
        self.metrics_visible = not self.metrics_visible
        self.metrics_bar.setVisible(self.metrics_visible)

    # Credential persistence ------------------------------------------
    def _load_credentials(self):
        try:
            u = keyring.get_password(SERVICE_ID, 'username')
            if u:
                self.user_edit.setText(u)
                p = keyring.get_password(SERVICE_ID, u)
                if p:
                    self.pass_edit.setText(p)
                    self.remember_chk.setChecked(True)
        except Exception:
            pass

    def _save_credentials(self, u: str, p: str):
        try:
            keyring.set_password(SERVICE_ID, 'username', u)
            keyring.set_password(SERVICE_ID, u, p)
        except Exception:
            pass

    # Lifecycle ------------------------------------------------------
    def closeEvent(self, event):  # type: ignore
        try:
            self.worker.stop()
        except Exception:
            pass
        try:
            self.thread.quit(); self.thread.wait(1000)
        except Exception:
            pass
        super().closeEvent(event)


def run():
    app = QApplication(sys.argv)
    win = MainWindow(); win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    run()
