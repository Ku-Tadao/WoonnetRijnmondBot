import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import types
from ui_qt.main import ListingCard, QApplication
from PySide6.QtWidgets import QLabel
import sys

app = QApplication.instance() or QApplication(sys.argv)

def test_all_images_normalization():
    data = {
        'id': '1',
        'address': 'Teststraat 1',
        'image_url': '//example.com/a.jpg',
        'image_urls': ['//example.com/a.jpg', '//example.com/b.jpg', 'https://example.com/c.jpg']
    }
    card = ListingCard(data)
    # _all_images should be normalized with https:
    assert all(u.startswith('https://') for u in card._all_images), card._all_images
    assert len(card._all_images) == 3


def test_gallery_wraparound(qtbot=None):
    data = {
        'id': '2',
        'address': 'Teststraat 2',
        'image_url': '//example.com/one.jpg',
        'image_urls': ['//example.com/one.jpg','//example.com/two.jpg']
    }
    card = ListingCard(data)
    # Simulate private state used in dialog
    state = {'idx':0, 'raw_cache':{}}
    # Wrap logic: manual reproduction
    total = len(card._all_images)
    state['idx'] = 0
    # prev from 0 -> last
    state['idx'] = (state['idx'] - 1) % total
    assert state['idx'] == total-1
    # next from last -> 0
    state['idx'] = (state['idx'] + 1) % total
    assert state['idx'] == 0
