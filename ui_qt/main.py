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
from PySide6.QtCore import Qt, QThread, Slot, Signal, QTimer, QObject, QAbstractAnimation
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
_GALLERY_CACHE_LOCK = threading.Lock()

class ListingCard(QFrame):
    imageLoaded = Signal(QPixmap)
    imageError = Signal()
    applyRequested = Signal(str)  # listing id

    _image_cache: 'OrderedDict[str, QPixmap]' = OrderedDict()
    _image_cache_limit = 100

    def __init__(self, data: Dict[str, Any], parent=None, session: requests.Session | None = None):
        super().__init__(parent)
        self.setObjectName("CardFrame")
        self.data = data
        self._session = session
        self._img_label: QLabel | None = None
        self._shadow: QGraphicsDropShadowEffect | None = None

        # Root row
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 14, 14, 14)
        row.setSpacing(16)

        # Image placeholder
        img_lbl = QLabel("IMG")
        img_lbl.setObjectName("cardImage")
        img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        img_lbl.setFixedSize(160, 120)
        row.addWidget(img_lbl)
        self._img_label = img_lbl
        self.imageLoaded.connect(self._apply_pixmap)
        self.imageError.connect(self._mark_image_error)
        img_lbl.mousePressEvent = self._on_image_clicked  # type: ignore

        # Middle column
        mid = QVBoxLayout(); mid.setSpacing(6)
        status_text = data.get('status_text', 'N/A')
        badge = QLabel(status_text)
        st = status_text.upper()
        badge.setObjectName(
            'statusBadgeLive' if 'LIVE' in st else (
                'statusBadgeSelectable' if 'SELECTABLE' in st else 'statusBadgePreview'
            )
        )
        raw_addr = (data.get('address') or '').strip()
        addr = QLabel(); addr.setObjectName('addressLabel'); addr.setTextFormat(Qt.TextFormat.RichText); addr.setWordWrap(True); addr.setText(f"<b>{raw_addr}</b>")
        meta_parts: List[str] = []
        if data.get('type'):
            meta_parts.append(data['type'])
        price_str = (data.get('price_str') or '').strip()
        if price_str:
            meta_parts.append(price_str)
        rooms_val = data.get('rooms')
        if rooms_val is not None:
            rv = str(rooms_val).strip()
            if rv and rv != '?' and rv.lower() != 'none':
                try:
                    n_rooms = int(float(rv))
                    rv_label = f"{n_rooms} room{'s' if n_rooms != 1 else ''}"
                except Exception:
                    rv_label = f"{rv} rooms"
                meta_parts.append(f"<span class='dim'>{rv_label}</span>")
        meta = QLabel('  ·  '.join(meta_parts)); meta.setObjectName('metaLabel'); meta.setTextFormat(Qt.TextFormat.RichText); meta.setWordWrap(True)
        mid.addWidget(badge); mid.addWidget(addr); mid.addWidget(meta)
        row.addLayout(mid, 1)

        # Right column (actions)
        right = QVBoxLayout(); right.setSpacing(6)
        details_btn = QPushButton("Details")

        def _open_details():
            dlg = QDialog(self)
            dlg.setWindowTitle("Details")
            v = QVBoxLayout(dlg)
            v.addWidget(QLabel(f"<b>{self.data.get('address','')}</b>"))
            pairs = self.data.get('meta_pairs') or []
            if pairs:
                for k, val in pairs:
                    v.addWidget(QLabel(f"{k}: {val}"))
            else:
                from pprint import pformat
                dump = QLineEdit(); dump.setReadOnly(True)
                dump.setText(pformat({k: self.data.get(k) for k in ('type','price_str','city','postcode','rooms','area_m2')}))
                v.addWidget(dump)
            close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            close.rejected.connect(dlg.reject)
            v.addWidget(close)
            dlg.exec()

        details_btn.clicked.connect(_open_details)
        right.addWidget(details_btn)
        apply_btn = QPushButton("Apply")
        apply_btn.setEnabled(bool(self.data.get('is_selectable')))
        apply_btn.clicked.connect(lambda: self.applyRequested.emit(str(self.data.get('id'))))
        right.addWidget(apply_btn)
        right.addStretch(1)
        row.addLayout(right)

        self.setFixedWidth(440)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setColor(QColor(0, 0, 0, 170))
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 4)
        self.setGraphicsEffect(shadow)
        self._shadow = shadow

        # Prepare images
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
            single = 'https:' + img_url if img_url.startswith('//') else img_url
            normalized.append(single)
        self._all_images = normalized
        logger.debug(f"[CARD] images count={len(self._all_images)} first={self._all_images[0] if self._all_images else None}")
        thumb_url: str | None = None
        if img_url:
            thumb_url = 'https:' + img_url if img_url.startswith('//') else img_url
        elif self._all_images:
            thumb_url = self._all_images[0]
        if thumb_url:
            _IMAGE_EXECUTOR.submit(self._load_image, thumb_url)
        else:
            logger.debug(f"[CARD] no image for id={data.get('id')} addr={data.get('address')}")

        if len(self._all_images) > 1:
            def _prefetch():
                time.sleep(0.25)
                for u in self._all_images:
                    if u == thumb_url:
                        continue
                    with _GALLERY_CACHE_LOCK:
                        if u in _GALLERY_CACHE:
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
                                with _GALLERY_CACHE_LOCK:
                                    _GALLERY_CACHE[u] = qimg
                                    if len(_GALLERY_CACHE) > _GALLERY_CACHE_LIMIT:
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
                    scaled = QPixmap.fromImage(img).scaled(
                        w, h,
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation
                    )
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
            anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
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
        if not getattr(self, '_all_images', None):
            logger.debug("[GALLERY] no images list empty")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Images")
        dlg.resize(820, 560)
        v = QVBoxLayout(dlg)
        title = QLabel(self.data.get('address',''))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)
        img_label = QLabel("Loading...")
        img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        img_label.setMinimumSize(400, 300)
        v.addWidget(img_label, 1)
        nav = QHBoxLayout()
        prev_btn = QPushButton("◀ Previous")
        next_btn = QPushButton("Next ▶")
        counter_lbl = QLabel("")
        nav.addWidget(prev_btn); nav.addStretch(1); nav.addWidget(counter_lbl); nav.addStretch(1); nav.addWidget(next_btn)
        v.addLayout(nav)
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close); v.addWidget(close_box); close_box.rejected.connect(dlg.reject)

        state = {'idx': 0, 'raw_cache': {}}  # per-dialog cache

        class _Bridge(QObject):
            deliver = Signal(str, QImage)

        bridge = _Bridge(dlg)

        def update_counter():
            counter_lbl.setText(f"{state['idx']+1} / {len(self._all_images)}")

        def scale_and_set(pix_raw: QPixmap):
            target_size = img_label.size()
            margin = 40
            sw = max(50, target_size.width()-margin)
            sh = max(50, target_size.height()-margin)
            scaled = pix_raw.scaled(sw, sh, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            img_label.setPixmap(scaled)
            img_label.setText("")

        def _on_deliver(u: str, qimg: QImage):
            if qimg.isNull():
                if self._all_images[state['idx']] == u:
                    img_label.setText("Invalid")
                return
            pix = QPixmap.fromImage(qimg)
            state['raw_cache'][u] = pix
            if self._all_images[state['idx']] == u:
                scale_and_set(pix)
        bridge.deliver.connect(_on_deliver)

        def load_index():
            update_counter()
            idx = state['idx']
            url = self._all_images[idx]
            img_label.setText("Loading...")
            raw_pix = state['raw_cache'].get(url)
            if raw_pix:
                scale_and_set(raw_pix); return
            with _GALLERY_CACHE_LOCK:
                global_img = _GALLERY_CACHE.get(url)
            if global_img:
                bridge.deliver.emit(url, global_img)
                return
            def worker():
                try:
                    sess = self._session if isinstance(self._session, requests.Session) else None
                    getter = sess.get if sess else requests.get
                    attempts = 3; last_status = None; r = None
                    for a in range(1, attempts+1):
                        try:
                            r = getter(url, timeout=10); last_status = r.status_code
                            if r.status_code == 200 and r.content:
                                break
                        except Exception as ie:
                            logger.debug(f"[GALLERY] attempt {a} error {ie} url={url}")
                        time.sleep(0.3 * a)
                    if not r or r.status_code != 200 or not r.content:
                        QTimer.singleShot(0, lambda s=last_status: img_label.setText(f"Failed ({s})")); return
                    qimg = QImage.fromData(r.content)
                    if qimg.isNull():
                        QTimer.singleShot(0, lambda: img_label.setText("Invalid")); return
                    bridge.deliver.emit(url, qimg)
                except Exception as e:
                    logger.debug(f"[GALLERY] fetch error {url}: {e}")
                    QTimer.singleShot(0, lambda: img_label.setText("Error"))
            threading.Thread(target=worker, daemon=True).start()

        def prev_clicked():
            state['idx'] = (state['idx'] - 1) % len(self._all_images); load_index()
        def next_clicked():
            state['idx'] = (state['idx'] + 1) % len(self._all_images); load_index()

        prev_btn.clicked.connect(prev_clicked)
        next_btn.clicked.connect(next_clicked)

        orig_resize = dlg.resizeEvent
        def resize_event(e):
            if img_label.pixmap():
                url = self._all_images[state['idx']]
                raw_pix = state['raw_cache'].get(url)
                if raw_pix:
                    scale_and_set(raw_pix)
            return orig_resize(e) if orig_resize else None
        dlg.resizeEvent = resize_event  # type: ignore

        from PySide6.QtGui import QKeyEvent
        orig_event = dlg.event
        def key_event(ev):
            if isinstance(ev, QKeyEvent):
                if ev.key() in (Qt.Key.Key_Left, Qt.Key.Key_A):
                    prev_clicked(); ev.accept(); return True
                if ev.key() in (Qt.Key.Key_Right, Qt.Key.Key_D):
                    next_clicked(); ev.accept(); return True
                if ev.key() == Qt.Key.Key_Escape:
                    dlg.reject(); ev.accept(); return True
            return orig_event(ev)
        dlg.event = key_event  # type: ignore

        QTimer.singleShot(50, load_index)
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
        self._thread = QThread(self)
        self.worker = BotWorker(self.client)
        self.worker.moveToThread(self._thread)
        self._thread.start()

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
        self.pass_edit = QLineEdit(); self.pass_edit.setPlaceholderText('Password'); self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
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
        self.metrics_bar = QLabel()
        self.metrics_bar.setObjectName('MetricsBar')
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
        base_dir = os.path.dirname(__file__)
        candidates = [
            os.path.join(base_dir, 'theme.qss'),  # source layout
            os.path.join(os.path.dirname(sys.executable), 'ui_qt', 'theme.qss'),  # one-file extracted
            os.path.join(os.getcwd(), 'ui_qt', 'theme.qss'),  # current working dir
        ]
        for p in candidates:
            if os.path.isfile(p):
                try:
                    with open(p, 'r', encoding='utf-8') as f:
                        self.setStyleSheet(f.read())
                    logger.debug(f"[THEME] Loaded stylesheet from {p}")
                    return
                except Exception as e:
                    logger.debug(f"[THEME] Failed to load {p}: {e}")
        logger.debug("[THEME] No stylesheet applied (all candidates missing)")

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
            card = ListingCard(data, session=self.client.session)
            card.applyRequested.connect(self._apply_one)
            self.cards_layout.addWidget(card)
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

    @Slot(str)
    def _apply_one(self, listing_id: str):
        # apply a single listing via worker
        self.statusBar().showMessage(f"Applying to {listing_id}...")
        import threading as _th
        _th.Thread(target=lambda: self.worker.apply([listing_id]), daemon=True).start()

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
            self._thread.quit(); self._thread.wait(1000)
        except Exception:
            pass
        super().closeEvent(event)


def run():
    app = QApplication(sys.argv)
    win = MainWindow(); win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    run()
