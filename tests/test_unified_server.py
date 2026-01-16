from __future__ import annotations

import tempfile
import textwrap
import os
import json
from pathlib import Path

from mcp_plus.fixture_server import serve_static_dir
from mcp_plus.stdio_client import MCPStdioClient
from tests._harness import _cleanup_session_processes  # type: ignore
from tests._harness import Harness
from tests._util import tool_json, tool_text


def _make_fixture_dir(tmp: Path) -> Path:
	root = tmp / "site"
	root.mkdir(parents=True, exist_ok=True)
	(root / "index.html").write_text(
		textwrap.dedent(
			"""\
			<!doctype html>
			<html lang="en">
			  <head>
			    <meta charset="utf-8" />
			    <title>MCP Plus Unified Fixture</title>
			  </head>
			  <body>
			    <h1>Unified Fixture</h1>
			    <p id="ok">ok</p>
			  </body>
			</html>
			"""
		),
		encoding="utf-8",
	)
	return root


def test_unified_smoke(_: Harness) -> None:
	repo_root = Path(__file__).resolve().parents[1]
	assert (repo_root / "bin").exists()
	python_bin = (os.environ.get("BROWSER_USE_MCP_PYTHON") or "").strip()
	assert python_bin, "Set BROWSER_USE_MCP_PYTHON for tests"

	session_id = "unified-test-suite"

	with tempfile.TemporaryDirectory(prefix="browser-use-mcp-plus-unified-tests-") as tmp:
		tmp_path = Path(tmp)
		state_dir = tmp_path / "state"
		profile_dir = tmp_path / "profiles"
		state_dir.mkdir(parents=True, exist_ok=True)
		profile_dir.mkdir(parents=True, exist_ok=True)

		fixture_root = _make_fixture_dir(tmp_path)

		common_env = {
			"BROWSER_USE_MCP_PYTHON": python_bin,
			"BROWSER_USE_SESSION_ID": session_id,
			"BROWSER_USE_CHROME_MODE": "session",
			"BROWSER_USE_ALLOW_HEADLESS_FALLBACK": "true",
			"BROWSER_USE_MCP_STATE_DIR": str(state_dir),
			"BROWSER_USE_CDP_PROFILE_BASE_DIR": str(profile_dir),
			# Keep tests offline/deterministic where possible.
			"OPENAI_API_KEY": "",
			"OPENAI_API_BASE": "",
			"OPENAI_BASE_URL": "",
			"CONTEXT7_API_KEY": "",
		}

		with serve_static_dir(fixture_root) as (url, url_contains):
			unified = MCPStdioClient(
				name="mcp-plus",
				command=[str(repo_root / "bin" / "unified_mcp.sh")],
				env=common_env,
				cwd=str(repo_root),
			)
			unified.start()
			try:
				unified.initialize()

				resp = unified.request("tools/list", {}, timeout_s=45.0)
				tools = (resp.get("result") or {}).get("tools") or []
				names = {t.get("name") for t in tools if isinstance(t, dict)}

				assert "browser-use.browser_navigate" in names
				assert "ui-describe.ui_describe" in names
				assert "chrome-devtools.evaluate_script" in names
				assert "context7_resolve_library_id" in names
				assert "docker_vm_run" in names
				assert "agent_s3_vm_selftest" in names
				assert "agent_s3_vm_run_task" in names

				# Proxy: navigate + evaluate title
				unified.request(
					"tools/call",
					{"name": "browser-use.browser_navigate", "arguments": {"url": url}},
					timeout_s=60.0,
				)
				title = unified.request(
					"tools/call",
					{
						"name": "chrome-devtools.evaluate_script",
						"arguments": {"url_contains": url_contains, "script": "document.title"},
					},
					timeout_s=45.0,
				)
				assert "MCP Plus Unified Fixture" in tool_text(title)

				# Internal: Context7 should error deterministically when not configured.
				c7 = unified.request(
					"tools/call",
					{
						"name": "context7_resolve_library_id",
						"arguments": {"libraryName": "react", "query": "hooks"},
					},
					timeout_s=45.0,
				)
				c7_text = tool_text(c7)
				assert c7_text, "Expected Context7 response text"

				# Internal: Docker runner (requires local Docker).
				docker = unified.request(
					"tools/call",
					{
						"name": "docker_vm_run",
						"arguments": {"image": "alpine:3.19", "command": "echo hello-from-docker"},
					},
					timeout_s=120.0,
				)
				out = tool_json(docker)
				assert out.get("exit_code") == 0
				assert "hello-from-docker" in (out.get("stdout") or "")

				# VM: Agent S3 environment selftest inside Docker (deterministic; no API key required).
				vm = unified.request(
					"tools/call",
					{
						"name": "agent_s3_vm_selftest",
						"arguments": {"repo_path": str(repo_root), "timeout_s": 900},
					},
					timeout_s=900.0,
				)
				vm_out = tool_json(vm)
				assert vm_out.get("run", {}).get("exit_code") == 0, vm_out
				vm_json = json.loads((vm_out.get("run", {}).get("stdout") or "").strip())
				assert vm_json.get("ok") is True, vm_json
			finally:
				try:
					unified.close()
				finally:
					_cleanup_session_processes(state_dir=state_dir, session_id=session_id)
