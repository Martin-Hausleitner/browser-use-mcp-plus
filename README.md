# browser-use-mcp-plus

Dieses Repo enthält Wrapper-Skripte und kleine MCP-Server, um eine lokale Chrome/Chromium-Instanz per CDP (DevTools Protocol) zuverlässig zu starten/zu finden und dann über MCP zu nutzen:

- `mcp-plus` (Unified MCP: proxy für `browser-use`/`ui-describe`/`chrome-devtools` + Context7 + Docker-Runner)
- `browser-use` (Upstream `browser_use.mcp` via Python)
- `ui-describe` (Screenshot → Textbeschreibung via Vision-LLM, mit Overlay-Cleanup)
- `chrome-devtools` (Network/Console/Performance/JS-eval via CDP)

## Struktur

- `bin/`: Entry-Points (werden von deinem MCP-Client aufgerufen)
- `lib/`: Shared Shell-Libs
- `mcp_plus/`: Python Helper-Module (stdio client, fixture server)
- `servers/`: Python MCP-Server (`ui_describe`, `chrome-devtools`)
- `scripts/`: Beispiele/Runner
- `tests/`: Mehrteilige lokale Test-Suite
- Runtime-State: standardmäßig unter `$XDG_STATE_HOME/browser-use-mcp-plus/` (oder `~/.local/state/browser-use-mcp-plus/`)

## Setup (Kurz)

1) Stelle sicher, dass `google-chrome` (oder `CHROME_BIN`) verfügbar ist und CDP genutzt werden darf.
2) Stelle sicher, dass dein Python-Interpreter die Dependencies hat (`browser_use`, `mcp`, etc.).
   - Setze dafür `BROWSER_USE_MCP_PYTHON=/pfad/zum/python` (z.B. ein venv), damit die Wrapper nicht hardcodiert sind.
3) Konfiguriere deinen MCP-Client so, dass er die Wrapper in `bin/` startet.

Beispiele ohne Secrets:
- Multi-Server (legacy): `examples/claude.mcpServers.example.json`
- Unified (empfohlen): `examples/claude.mcpServers.unified.example.json`

## Quickstart

- Example (öffnet eine lokale Fixture-Seite, navigiert via `browser-use`, beschreibt via `ui-describe`, eval via `chrome-devtools`):
  - `bin/mcp_plus.sh example`
- Test-Suite (mehrere Tests; smoke + overlay-cleanup + console/network + keep-open):
  - `bin/mcp_plus.sh test`

Mehr Doku:
- `docs/CONFIGURATION.md`
- `docs/TESTING.md`
- `docs/TROUBLESHOOTING.md`

## Wichtige Env-Variablen

- `BROWSER_USE_MCP_PYTHON`: Python-Executable für alle Wrapper (Default: `python3`)
- `BROWSER_USE_MCP_STATE_DIR`: Root für Runtime-State (Sessions/PIDs/Logs); Default: `~/.local/state/browser-use-mcp-plus`
- `BROWSER_USE_CHROME_MODE`: `session` (default) | `persistent` | `auto`
- `CHROME_BIN`: Chrome/Chromium Binary (Default: `google-chrome`)
- `CDP_HOST` / `CDP_PORT`: Host/Port für persistent mode (Default: `127.0.0.1:9222`)

## Security

Dieses Repo enthält bewusst **keine** Tokens/Keys. Lege Secrets nur in deinem MCP-Client-Config/Secret-Store ab und committe sie nicht.
