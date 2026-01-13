# Configuration

## Runtime state

Default state directory (sessions, PIDs, logs):

- `$BROWSER_USE_MCP_STATE_DIR` (if set)
- else `$XDG_STATE_HOME/browser-use-mcp-plus`
- else `~/.local/state/browser-use-mcp-plus`

## Python

Wrappers run Python from:

1) `$BROWSER_USE_MCP_PYTHON` (recommended)
2) `python3` / `python` (must have deps installed)

## Chrome mode

- `BROWSER_USE_CHROME_MODE=session` (default): isolated profile + random CDP port per session id
- `BROWSER_USE_CHROME_MODE=persistent`: fixed `CDP_HOST`/`CDP_PORT` (default `127.0.0.1:9222`)
- `BROWSER_USE_CHROME_MODE=auto`: uses `persistent` unless `BROWSER_USE_SESSION_ID` is set

## Headless fallback

If `$DISPLAY` is not set, `bin/ensure_cdp_chrome.sh` refuses to start headless Chrome unless:

- `BROWSER_USE_ALLOW_HEADLESS_FALLBACK=true`

## Common env vars

- `BROWSER_USE_SESSION_ID`: stable key for the session state folder
- `BROWSER_USE_MCP_STATE_DIR`: root for sessions/pids/logs
- `BROWSER_USE_CDP_PROFILE_BASE_DIR`: base directory for session Chrome profiles
- `CHROME_BIN`: chrome/chromium executable (default `google-chrome`)
- `CDP_HOST` / `CDP_PORT`: persistent mode CDP endpoint

