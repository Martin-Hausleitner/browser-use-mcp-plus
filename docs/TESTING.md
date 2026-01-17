# Testing

## Local test suite

Runs a local static fixture server, starts all MCP servers via stdio, navigates with `browser-use`, then verifies:

- MCP init + `tools/list`
- `chrome-devtools` JS eval + console + network capture
- `ui-describe` overlay cleanup + deterministic fallback (no LLM configured)
- `set_browser_keep_open`
- Unified server (`bin/unified_mcp.sh`) tool proxying + internal tools (Context7 config error, Docker runner)
- Agent S3 VM selftest (Docker image build + Xvfb + accessibility tree)

Command:

```bash
cd ~/.browser-use-mcp-plus
BROWSER_USE_MCP_PYTHON=/path/to/venv/bin/python bin/mcp_plus.sh test
```

## Example

```bash
cd ~/.browser-use-mcp-plus
BROWSER_USE_MCP_PYTHON=/path/to/venv/bin/python bin/mcp_plus.sh example
```

## Live (real LLM) E2E

Runs a real tool-using LLM loop against the unified MCP server:

- Context7 research (`context7_resolve_library_id`, `context7_query_docs`)
- UI validation via screenshot (`ui-describe.ui_describe`)
- UI fixes applied to a local fixture (`index.html`, `styles.css`)
- Deterministic verification via `chrome-devtools.evaluate_script` (no overlap + WCAG-ish contrast)

Required env:

- `BROWSER_USE_MCP_PYTHON` (python with `mcp`, `browser-use`, `playwright`, etc.)
- `OPENAI_BASE_URL` (e.g. `https://api.openai.com/v1`) or `OPENAI_API_BASE`
- `OPENAI_API_KEY` (or a valid `~/.codex/auth.json` from `codex login`)
- `CONTEXT7_API_KEY` (or `~/.codex/config.toml` `mcp_servers.context7` `--api-key …`)
- Optional: `MCP_PLUS_LIVE_MODEL` / `--model` (defaults to `gpt-4o-mini`)
- Optional: `UI_VISION_MODEL` (defaults to the same as `--model`)

Command:

```bash
cd ~/.browser-use-mcp-plus
export BROWSER_USE_MCP_PYTHON=/path/to/venv/bin/python
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=https://api.openai.com/v1
export CONTEXT7_API_KEY=...
bin/mcp_plus.sh test-live
```
