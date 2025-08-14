# WoonnetRijnmondBot
Modern Python bot to assist with discovery and on‑time applications for WoonnetRijnmond listings.
## UI (Qt / PySide6)
The interface is a PySide6 (Qt) application located in `ui_qt/main.py`, offering:
- Responsive card layout (custom FlowLayout) with hover elevation shadows
- Fade‑in thumbnail animation & robust threaded loading (LRU cache + retries)
- Multi‑image gallery dialog:
	- Keyboard navigation (←/→ or A/D, Esc to close, wrap‑around)
	- Prefetch & global gallery cache (stores QImage; converted to QPixmap on GUI thread)
	- Retry with incremental backoff & clear failure labeling (e.g. 520 responses)
	- Immediate re-display when navigating back (signal bridge)
- Distinct status badges (PREVIEW / SELECTABLE / LIVE)
- Secure credential storage via `keyring`
- Shared authenticated `requests.Session` (Selenium bootstrap → all HTTP/image fetches)
- Structured logging in `qt_ui.log` (thumbnails, gallery, prefetch diagnostics)

Legacy Tk / ttkbootstrap UI has been fully removed (simplifies dependencies and build size).
## Core Features

- Headless Selenium login with cookie transfer to persistent session
- Smart listing discovery (change detection & ID cache persistence)
- Parallel detail fetching & application submission
- Adaptive refresh cadence (time‑aware)
- Server countdown integration prior to application window
- Debug instrumentation toggle for HTTP tracing
- Discord error reporting hook (`reporting.py`)
## Architecture

```
main.py (entry -> Qt)
 ├─ ui_qt/main.py (Qt UI, gallery, image caches)
 └─ core/automation.py (WoonnetClient: login, discover, apply, metrics)
```

### Responsibilities

- Core (`core/automation.py`): Automation + HTTP + parsing; metrics & debug toggles. Supports `enable_browser=False` or `WOONNET_NO_BROWSER=1` for headless test runs without Selenium.
- Qt UI (`ui_qt/main.py`): Rich interactive UX (cards, gallery, prefetch, metrics text, credential persistence).
- Entry (`main.py`): Launches Qt UI.
 (Legacy Tk UI removed.)

## Running (Source)

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py          # launches Qt UI
```

Direct module execution:

```powershell
python -m ui_qt.main    # Qt UI (alternate invocation)
```
### Environment Variables

| Variable | Purpose | Typical Use |
|----------|---------|-------------|
| `WOONNET_NO_BROWSER=1` | Skip Selenium driver init (no Chrome needed) | Running unit tests / CI |
| `PYTHONWARNINGS=ignore` | Suppress noisy warnings | Optional |
Example (PowerShell):

```powershell
$env:WOONNET_NO_BROWSER=1; pytest -q
```
## Building Executable (PyInstaller)

```powershell
pyinstaller --noconfirm --clean --icon assets/icon.ico --name WoonnetRijnmondBot main.py
```

Ensure spec includes icon + any future Qt resources.
## Debug Mode (Core Client)

Enable in code:

```python
client.set_debug(True)
client.set_http_body_trace(False)  # to suppress body dumps
```
## Data Flow (Qt)

1. Login triggers Selenium headless browser → cookies transferred to `requests.Session`.
2. Core discovery posts API payload → listing IDs & details (parallel) → enriched listing dicts.
3. Qt UI renders `ListingCard` widgets; thumbnail fetchers run in thread pool.
4. User opens gallery; prefetch cache serves or background fetch retries.
5. On application window open / countdown complete, parallel submissions executed.
## Testing

```powershell
$env:WOONNET_NO_BROWSER=1; pytest -q
```

Existing focus:
- Price & publication parsing
- ID cache persistence
- Gallery logic (URL normalization & wrap-around navigation)

Suggested additions:
- Discovery change detection (added/removed IDs)
- Gallery retry and 520 edge cases (mocked)
- Prefetch cache eviction behavior
- Application form parsing variants
## Development Workflow

1. Create / activate venv
2. Install deps `pip install -r requirements.txt`
3. Run `python main.py` (Qt UI)
4. Enable debug (future menu toggle) or via client API
5. Tests: `WOONNET_NO_BROWSER=1 pytest -q`
6. Package with PyInstaller when ready
## Credentials Handling

- Stored securely via `keyring` (no plaintext file)
- Clear via OS credential manager if needed
## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Chrome fails to start | Driver mismatch or blocked | Clear driver cache; ensure Chrome installed |
| NO IMAGE thumbnails | 4xx / invalid content / auth | Check `qt_ui.log`; verify login state |
| Gallery 520 failures | CDN edge responses | Navigate again; retries logged; potential future jitter backoff |
| Excessive logs | Debug mode enabled | Disable via `client.set_debug(False)` |
| Slow tests | Browser spawning | Use `WOONNET_NO_BROWSER=1` |
## Contributing

1. Add / update tests for behavioral changes
2. Run tests headless (`WOONNET_NO_BROWSER=1`)
3. Keep UI vs core separation (no Selenium in `ui_qt/`)
4. Don’t hardcode secrets / webhooks
## Automated Builds

Configure CI to run tests with `WOONNET_NO_BROWSER=1` and build with PyInstaller targeting `main.py`.
## Disclaimer

Use responsibly. Respect site terms of service. No guarantees of availability, stability, or correctness.
