from __future__ import annotations

import contextlib
import functools
import http.server
import os
import threading
import tempfile
import textwrap
from pathlib import Path

from ._mcp_stdio_client import MCPStdioClient


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
	def log_message(self, format: str, *args) -> None:  # noqa: A002
		return


@contextlib.contextmanager
def _serve_fixture() -> tuple[str, str]:
	with tempfile.TemporaryDirectory(prefix="mcp-plus-fixture-") as tmp:
		root = Path(tmp)
		(root / "index.html").write_text(
			textwrap.dedent(
				"""\
				<!doctype html>
				<html lang="en">
				  <head>
				    <meta charset="utf-8" />
				    <meta name="viewport" content="width=device-width, initial-scale=1" />
				    <title>MCP Plus Fixture</title>
				    <style>
				      body { font-family: system-ui, sans-serif; margin: 32px; }
				      .card { border: 1px solid #ddd; border-radius: 12px; padding: 16px; max-width: 520px; }
				      button { padding: 10px 14px; border-radius: 10px; border: 1px solid #ccc; cursor: pointer; }
				    </style>
				  </head>
				  <body>
				    <h1>browser-use-mcp-plus</h1>
				    <div class="card">
				      <p id="msg">Hello from the local fixture page.</p>
				      <button onclick="document.getElementById('msg').textContent='Clicked!'">Click me</button>
				    </div>
				  </body>
				</html>
				"""
			),
			encoding="utf-8",
		)

		handler = functools.partial(_QuietHandler, directory=str(root))
		with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as httpd:
			port = httpd.server_address[1]
			url = f"http://127.0.0.1:{port}/"
			thread = threading.Thread(target=httpd.serve_forever, name="fixture-httpd", daemon=True)
			thread.start()

			try:
				yield url, f"127.0.0.1:{port}"
			finally:
				httpd.shutdown()
				thread.join(timeout=2)


def main() -> int:
	repo_root = Path(__file__).resolve().parents[1]
	session_id = os.getenv("BROWSER_USE_SESSION_ID", "example")

	with _serve_fixture() as (url, url_contains):
		common_env = {
			"BROWSER_USE_SESSION_ID": session_id,
			"BROWSER_USE_CHROME_MODE": os.getenv("BROWSER_USE_CHROME_MODE", "session"),
			"BROWSER_USE_ALLOW_HEADLESS_FALLBACK": os.getenv("BROWSER_USE_ALLOW_HEADLESS_FALLBACK", "true"),
		}

		browser_use = MCPStdioClient(
			name="browser-use",
			command=[str(repo_root / "bin" / "browser_use_mcp.sh")],
			env=common_env,
			cwd=str(repo_root),
		)
		browser_use.start()
		try:
			browser_use.initialize()
			tools = browser_use.request("tools/list", {}, timeout_s=20.0)
			print(f"[browser-use] tools={len((tools.get('result') or {}).get('tools') or [])}")
			browser_use.request("tools/call", {"name": "browser_navigate", "arguments": {"url": url}}, timeout_s=45.0)
		finally:
			browser_use.close()

		ui = MCPStdioClient(
			name="ui-describe",
			command=[str(repo_root / "bin" / "ui_describe_mcp.sh")],
			env=common_env,
			cwd=str(repo_root),
		)
		ui.start()
		try:
			ui.initialize()
			resp = ui.request(
				"tools/call",
				{
					"name": "ui_describe",
					"arguments": {"url_contains": url_contains, "max_chars": 300, "question": "Describe the UI briefly."},
				},
				timeout_s=45.0,
			)
			text = (((resp.get("result") or {}).get("content") or [{}])[0] or {}).get("text") or ""
			print(f"[ui-describe] {text.splitlines()[0] if text else 'no output'}")
		finally:
			ui.close()

		devtools = MCPStdioClient(
			name="chrome-devtools",
			command=[str(repo_root / "bin" / "chrome_devtools_mcp.sh")],
			env=common_env,
			cwd=str(repo_root),
		)
		devtools.start()
		try:
			devtools.initialize()
			resp = devtools.request(
				"tools/call",
				{"name": "evaluate_script", "arguments": {"url_contains": url_contains, "script": "document.title"}},
				timeout_s=30.0,
			)
			text = (((resp.get("result") or {}).get("content") or [{}])[0] or {}).get("text") or ""
			print(f"[chrome-devtools] document.title => {text.strip()}")
		finally:
			devtools.close()

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
