import os, json, tempfile, logging
from queue import Queue
from core.automation import WoonnetClient

# Minimal logger
logger = logging.getLogger('test'); logger.addHandler(logging.StreamHandler()); logger.setLevel(logging.DEBUG)

def make_client(tmpdir):
    q = Queue()
    return WoonnetClient(q, logger, log_file_path=os.path.join(tmpdir, 'log.txt'), enable_browser=False)

FAKE_LISTING_DETAIL = {
    'id': '123', 'straat': 'Teststraat', 'huisnummer': '10', 'objecttype': 'Appartement',
    'kalehuur': '750,00', 'publstart': '/Date(1700000000000)/', 'media': [{'type':'StraatFoto','fotoviewer':'//img.example.com/a.jpg'}]
}

def test_parse_price_and_publ_date():
    with tempfile.TemporaryDirectory() as td:
        c = make_client(td)
        assert c._parse_price('€ 1.234,56') == 1234.56
        assert c._parse_price('') == 0.0
        assert c._parse_publ_date(None) is None
        assert c._parse_publ_date('/Date(1700000000000)/') is not None

def test_cache_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        c = make_client(td)
        c.set_cache_path(td)
        c._last_listing_ids = {'1','2','3'}
        c._persist_ids()
        # new client loads
        c2 = make_client(td)
        c2.set_cache_path(td)
        c2.load_cached_ids()
        assert c2._last_listing_ids == {'1','2','3'}

def test_cookie_persistence_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        c = make_client(td)
        c.set_cache_path(td)
        # Simulate a login-set cookie
        c.session.cookies.set('TestCookie', 'ABC123', domain='example.com', path='/')
        c.save_cookies()
        # New client should load it
        c2 = make_client(td)
        c2.set_cache_path(td)
        assert c2.session.cookies.get('TestCookie') == 'ABC123'

def test_debug_patch_and_unpatch():
    with tempfile.TemporaryDirectory() as td:
        c = make_client(td)
        orig = c.session.request
        c.set_debug(True)
        assert c._orig_request_func is not None
        assert c.session.request is not orig  # wrapped
        c.set_debug(False)
        # After disabling, request should be original (or functionally equivalent)
        assert c._orig_request_func is None
