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

## Unified MCP Server (`bin/unified_mcp.sh`)

Der Unified-Server exposed die Tools aus `browser-use`, `ui-describe` und `chrome-devtools` unter Namespaces:

- `browser-use.<tool>`
- `ui-describe.<tool>`
- `chrome-devtools.<tool>`

Optional kannst du Child-Server deaktivieren:

- `MCP_PLUS_ENABLE_BROWSER_USE=true|false`
- `MCP_PLUS_ENABLE_UI_DESCRIBE=true|false`
- `MCP_PLUS_ENABLE_CHROME_DEVTOOLS=true|false`

### Context7

- `CONTEXT7_API_KEY`: API Key (empfohlen; ohne Key gibt es einen deterministischen Fehler)
- `CONTEXT7_BASE_URL`: Default `https://context7.com`
- `CONTEXT7_ALLOW_UNAUTHENTICATED=true`: versucht Calls ohne Key (nicht empfohlen)

### Docker VM Runner

Der Tool `docker_vm_run` ruft lokal `docker` auf und startet einen ephemeren Container.
Voraussetzung: laufender Docker-Daemon + Zugriff für den User.
