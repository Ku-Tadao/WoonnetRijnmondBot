import threading, time
from typing import List, Dict, Any, Sequence
from PySide6.QtCore import QObject, Signal, Slot
from core.automation import WoonnetClient

class BotWorker(QObject):
    status = Signal(str)
    listings = Signal(list)
    metrics = Signal(dict)
    loggedIn = Signal(bool)

    def __init__(self, client: WoonnetClient):
        super().__init__()
        self.client = client
        self._metrics_timer = None
        self._running = False

    @Slot(str, str)
    def login(self, username: str, password: str):
        ok, session = self.client.login(username, password)
        self.loggedIn.emit(ok)
        if ok and not self._running:
            self._running = True
            self._start_metrics_loop()

    @Slot()
    def discover(self):
            listings = self.client.discover_listings_api()
            # Always emit (even empty) so UI can refresh state when no new listings
            self.listings.emit(listings or [])

    @Slot(list)
    def apply(self, listing_ids: Sequence[str]):
        self.client.apply_to_listings(listing_ids)

    def _start_metrics_loop(self):
        def loop():
            while self._running:
                try:
                    self.metrics.emit(self.client.get_metrics())
                except Exception:
                    pass
                time.sleep(1.5)
        threading.Thread(target=loop, daemon=True).start()

    def stop(self):
        """Signal loops/threads to stop."""
        self._running = False
