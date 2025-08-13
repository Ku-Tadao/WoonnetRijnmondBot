# WoonnetRijnmondBot

Modular Python bot to assist with discovery and on-time applications for
WoonnetRijnmond listings. The project has been refactored into clear layers:

- `core/automation.py` → Headless browser + API client (`WoonnetClient`).
- `ui/app.py` → Modern `ttkbootstrap` GUI (`App`).
- `main.py` → Minimal entrypoint launching the UI.
-- Legacy shims removed (`bot.py`, `hybrid_bot.py`); import directly from new modules.

## Features

- Headless Selenium login with cookie handoff to a fast `requests` session.
- Smart listing discovery with change detection and cached ID persistence.
- Parallel detail fetching & application submission for speed.
- Adaptive refresh cadence (slows when far from 20:00, accelerates near window).
- Precise server-side countdown usage (when API provides timer).
- Rich debug mode: full HTTP request/response tracing (toggleable body dump).
- GUI listing cards with status (PREVIEW / SELECTABLE / LIVE) and image thumbnails.
- Secure credential storage via `keyring`.
- Discord error reporting hook (see `reporting.py`).

## Running (Source)

```bash
python -m pip install -r requirements.txt
python main.py
```

Or directly:

```bash
python -m ui.app
```

Legacy shim entrypoints have been removed; use `python main.py` (preferred) or `python -m ui.app`.

### Quick Start (Windows PowerShell)

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
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

## Building Executable

Use the provided PyInstaller spec (`WoonnetRijnmondBot.spec`) or create a new one targeting `main.py`.

Example (PowerShell):

```powershell
pyinstaller --noconfirm --clean --icon assets/icon.ico --name WoonnetRijnmondBot main.py
```

If you add or rename modules, regenerate the spec (or edit existing) so `main.py` is the entrypoint. Include `assets/icon.ico` in the spec if not already.

## Debug Mode

Enable via View → Debug Mode (GUI). While enabled:

- Each HTTP request logs method, URL, headers, and optional JSON/data snippet.
- Each response logs status, size, duration, and (if enabled) a body snippet.
- Toggle body dumps by calling `set_http_body_trace(False)` on the client if needed.

Programmatic example:

```python
from core.automation import WoonnetClient
from queue import Queue
import logging

client = WoonnetClient(Queue(), logging.getLogger('woonnet'), 'bot.log')
client.set_debug(True)              # enable HTTP tracing
client.set_http_body_trace(False)   # suppress response body snippets
```

## Architecture Overview

```
main.py
 ├─ ui/app.py (UI: App, ListingCard)
 └─ core/automation.py (WoonnetClient: login, discover, apply)
```

Data Flow:
1. UI triggers login → headless Selenium session → cookies transferred to `requests`.
2. Discovery posts API filter → returns listing IDs → parallel detail fetch.
3. Listings rendered in UI with selection toggles; cached IDs persisted (avoid noise).
4. Application queue waits for server timer or 20:00 → parallel form submissions.

### Layer Responsibilities

- Core (`core/automation.py`): Pure automation + HTTP + parsing. Exposes toggles and helper methods; now supports `enable_browser=False` / `WOONNET_NO_BROWSER=1` to facilitate fast tests without a Chrome driver.
- UI (`ui/app.py`): Tk/ttkbootstrap interface (listing cards, auto-refresh logic, logging surface, selection UX, debug toggle). Calls into core; no scraping logic lives here.
- Shims: Removed to simplify codebase (previously `bot.py`, `hybrid_bot.py`).
- Entry (`main.py`): Minimal launcher for end users and packaging.

## Migration Notes

If you previously imported `WoonnetBot` from `bot`, update to:

```python
from core.automation import WoonnetClient
```

`WoonnetBot` remains an alias in `core.automation` only.

To bypass Selenium (e.g., CI tests), either:

```python
client = WoonnetClient(queue, logger, log_file_path='bot.log', enable_browser=False)
```

or set environment variable:

```powershell
$env:WOONNET_NO_BROWSER=1
```

Then only non-browser dependent methods (parsing utilities, cache persistence) are exercised.

## Testing

Unit tests live in `tests/`. Current coverage focuses on:

- Price parsing and publication date parsing.
- ID cache persistence (`last_ids.json`).
- Debug tracing patch/unpatch (ensures HTTP request wrapper attaches/detaches cleanly).

Run all tests (headless / no browser):

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:WOONNET_NO_BROWSER=1; pytest -q
```

Add more tests by mocking HTTP (e.g., with `responses` or `requests-mock`) to simulate API discovery payloads and application form HTML.

### Suggested Future Tests

- Mocked `discover_listings_api` sequence demonstrating change detection (+/- IDs).
- Form extraction edge cases (missing token, multiple forms).
- Auto-refresh scheduling intervals (extract logic into pure function for deterministic tests).

## Development Workflow

1. Create / activate virtual environment.
2. Install dependencies from `requirements.txt`.
3. Run `python main.py` (GUI) or write scripts against `WoonnetClient`.
4. Enable debug mode via GUI (View → Debug Mode) for verbose HTTP tracing.
5. Run tests with `pytest -q` (set `WOONNET_NO_BROWSER=1` for speed).
6. Before packaging, clear old `dist/` & `build/` directories (PyInstaller) to avoid stale artifacts.

## Credentials Handling

- Stored securely via `keyring` when you check "Remember credentials".
- To clear saved values: remove the service entries through your OS credential manager or uncheck and restart (no plaintext file is used).

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Chrome fails to start | Missing / incompatible driver | Delete driver cache; re-run (webdriver-manager will fetch). |
| Empty listings repeatedly | Site not publishing new data yet | Keep window open; adaptive refresh slows polling when far from 20:00. |
| Flood of HTTP logs | Debug mode on + body tracing | Disable via GUI or `client.set_debug(False)` / `set_http_body_trace(False)`. |
| Tests hang | Browser starting during tests | Set `WOONNET_NO_BROWSER=1` or pass `enable_browser=False`. |

## Contributing

Pull requests welcome. Please:

1. Add / update tests for behavioral changes.
2. Run `pytest -q` (with `WOONNET_NO_BROWSER=1`).
3. Keep UI / core separation: no Selenium or HTTP logic inside `ui/`.
4. Avoid leaking secrets—Discord reporting uses a proxy; do not hardcode webhooks.

---

If any step here seems outdated or unclear, open an issue so the README stays accurate.

## Automated Builds

If you rely on GitHub Actions releases, ensure the workflow now invokes `python main.py` (or module `ui.app`). Update the spec file if needed to reflect new imports.

## Disclaimer

Use responsibly. Respect site terms of service. No guarantees of availability or correctness.
