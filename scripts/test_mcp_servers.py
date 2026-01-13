from __future__ import annotations

import contextlib
import os
import tempfile
import textwrap
from pathlib import Path

from mcp_plus.fixture_server import serve_static_dir
from mcp_plus.stdio_client import MCPStdioClient


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
				    <title>MCP Plus Test Fixture</title>
				  </head>
				  <body>
				    <h1>Fixture</h1>
				    <p id="ok">ok</p>
				  </body>
				</html>
				"""
			),
			encoding="utf-8",
		)

		with serve_static_dir(root) as (url, url_contains):
			yield url, url_contains


def _assert(cond: bool, msg: str) -> None:
	if not cond:
		raise AssertionError(msg)


def main() -> int:
	repo_root = Path(__file__).resolve().parents[1]
	session_id = os.getenv("BROWSER_USE_SESSION_ID", "test-suite")

	with _serve_fixture() as (url, url_contains):
		common_env = {
			"BROWSER_USE_SESSION_ID": session_id,
			"BROWSER_USE_CHROME_MODE": os.getenv("BROWSER_USE_CHROME_MODE", "session"),
			"BROWSER_USE_ALLOW_HEADLESS_FALLBACK": os.getenv("BROWSER_USE_ALLOW_HEADLESS_FALLBACK", "true"),
		}

		# 1) browser-use
		browser_use = MCPStdioClient(
			name="browser-use",
			command=[str(repo_root / "bin" / "browser_use_mcp.sh")],
			env=common_env,
			cwd=str(repo_root),
		)
		browser_use.start()
		try:
			browser_use.initialize()
			tools_resp = browser_use.request("tools/list", {}, timeout_s=20.0)
			tools = (tools_resp.get("result") or {}).get("tools") or []
			names = {t.get("name") for t in tools if isinstance(t, dict)}
			_assert("browser_navigate" in names, "browser-use missing tool: browser_navigate")

			nav = browser_use.request(
				"tools/call",
				{"name": "browser_navigate", "arguments": {"url": url}},
				timeout_s=45.0,
			)
			_assert("error" not in nav, f"browser_navigate failed: {nav.get('error')}")
		finally:
			browser_use.close()

		# 2) ui-describe
		ui = MCPStdioClient(
			name="ui-describe",
			command=[str(repo_root / "bin" / "ui_describe_mcp.sh")],
			env=common_env,
			cwd=str(repo_root),
		)
		ui.start()
		try:
			ui.initialize()
			tools_resp = ui.request("tools/list", {}, timeout_s=20.0)
			tools = (tools_resp.get("result") or {}).get("tools") or []
			names = {t.get("name") for t in tools if isinstance(t, dict)}
			_assert("ui_describe" in names, "ui-describe missing tool: ui_describe")

			desc = ui.request(
				"tools/call",
				{
					"name": "ui_describe",
					"arguments": {"url_contains": url_contains, "max_chars": 200, "question": "What do you see?"},
				},
				timeout_s=45.0,
			)
			text = (((desc.get("result") or {}).get("content") or [{}])[0] or {}).get("text") or ""
			_assert(not text.lstrip().startswith("Error:"), f"ui_describe returned error text: {text[:200]}")
		finally:
			ui.close()

		# 3) chrome-devtools
		devtools = MCPStdioClient(
			name="chrome-devtools",
			command=[str(repo_root / "bin" / "chrome_devtools_mcp.sh")],
			env=common_env,
			cwd=str(repo_root),
		)
		devtools.start()
		try:
			devtools.initialize()
			tools_resp = devtools.request("tools/list", {}, timeout_s=20.0)
			tools = (tools_resp.get("result") or {}).get("tools") or []
			names = {t.get("name") for t in tools if isinstance(t, dict)}
			_assert("evaluate_script" in names, "chrome-devtools missing tool: evaluate_script")

			ev = devtools.request(
				"tools/call",
				{"name": "evaluate_script", "arguments": {"url_contains": url_contains, "script": "document.title"}},
				timeout_s=30.0,
			)
			text = (((ev.get("result") or {}).get("content") or [{}])[0] or {}).get("text") or ""
			_assert("MCP Plus Test Fixture" in text, f"evaluate_script unexpected result: {text[:200]}")
		finally:
			devtools.close()

	print("PASS: browser-use, ui-describe, chrome-devtools")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
