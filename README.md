# browser-use MCP helpers (local)

Dieses Repo enthält Wrapper-Skripte und kleine MCP-Server, um eine lokale Chrome/Chromium-Instanz per CDP (DevTools Protocol) zuverlässig zu starten/zu finden und dann über MCP zu nutzen:

- `browser-use` (Upstream `browser_use.mcp` via Python)
- `ui-describe` (Screenshot → Textbeschreibung via Vision-LLM, mit Overlay-Cleanup)
- `chrome-devtools` (Network/Console/Performance/JS-eval via CDP)

## Struktur

- `bin/`: Entry-Points (werden von deinem MCP-Client aufgerufen) + Shared Shell-Libs
- `servers/`: Python MCP-Server (`ui_describe`, `chrome-devtools`)
- `sessions/`: Laufzeit-State pro Session (ignored; wird automatisch erzeugt)

## Setup (Kurz)

1) Stelle sicher, dass `google-chrome` (oder `CHROME_BIN`) verfügbar ist und CDP genutzt werden darf.
2) Stelle sicher, dass dein Python-Interpreter die Dependencies hat (`browser_use`, `mcp`, etc.).
   - Setze dafür `BROWSER_USE_MCP_PYTHON=/pfad/zum/python` (z.B. ein venv), damit die Wrapper nicht hardcodiert sind.
3) Konfiguriere deinen MCP-Client so, dass er die Wrapper in `bin/` startet.

Beispiel-Snippet ohne Secrets: `examples/claude.mcpServers.example.json`.

## Wichtige Env-Variablen

- `BROWSER_USE_MCP_PYTHON`: Python-Executable für alle Wrapper (Default: `python3`)
- `BROWSER_USE_CHROME_MODE`: `session` (default) | `persistent` | `auto`
- `CHROME_BIN`: Chrome/Chromium Binary (Default: `google-chrome`)
- `CDP_HOST` / `CDP_PORT`: Host/Port für persistent mode (Default: `127.0.0.1:9222`)

## Security

Dieses Repo enthält bewusst **keine** Tokens/Keys. Lege Secrets nur in deinem MCP-Client-Config/Secret-Store ab und committe sie nicht.

