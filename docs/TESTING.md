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
