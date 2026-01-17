from __future__ import annotations

import base64
import contextlib
import tempfile
import textwrap
import os
import json
import socket
import subprocess
import time
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


@contextlib.contextmanager
def _start_unified(
	*,
	repo_root: Path,
	python_bin: str,
	session_id: str,
	extra_env: dict[str, str] | None = None,
) -> contextlib.Iterator[tuple[MCPStdioClient, Path]]:
	with tempfile.TemporaryDirectory(prefix="browser-use-mcp-plus-unified-tests-") as tmp:
		tmp_path = Path(tmp)
		state_dir = tmp_path / "state"
		profile_dir = tmp_path / "profiles"
		state_dir.mkdir(parents=True, exist_ok=True)
		profile_dir.mkdir(parents=True, exist_ok=True)

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
		if extra_env:
			common_env.update(extra_env)

		unified = MCPStdioClient(
			name="mcp-plus",
			command=[str(repo_root / "bin" / "unified_mcp.sh")],
			env=common_env,
			cwd=str(repo_root),
		)
		unified.start()
		try:
			unified.initialize()
			yield unified, state_dir
		finally:
			try:
				unified.close()
			finally:
				_cleanup_session_processes(state_dir=state_dir, session_id=session_id)


def test_unified_smoke(_: Harness) -> None:
	repo_root = Path(__file__).resolve().parents[1]
	assert (repo_root / "bin").exists()
	python_bin = (os.environ.get("BROWSER_USE_MCP_PYTHON") or "").strip()
	assert python_bin, "Set BROWSER_USE_MCP_PYTHON for tests"

	session_id = "unified-test-suite"

	with tempfile.TemporaryDirectory(prefix="browser-use-mcp-plus-unified-fixture-") as tmp:
		tmp_path = Path(tmp)
		fixture_root = _make_fixture_dir(tmp_path)

		with serve_static_dir(fixture_root) as (url, url_contains):
			with _start_unified(repo_root=repo_root, python_bin=python_bin, session_id=session_id) as (unified, _state_dir):
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
				assert "CONTEXT7_API_KEY is not set" in c7_text

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
				assert "Repo present:" in (vm_out.get("run", {}).get("stderr") or "")
				assert "Cloning repo:" not in (vm_out.get("run", {}).get("stderr") or "")
				vm_json = json.loads((vm_out.get("run", {}).get("stdout") or "").strip())
				assert vm_json.get("ok") is True, vm_json


def test_unified_internal_errors(_: Harness) -> None:
	repo_root = Path(__file__).resolve().parents[1]
	python_bin = (os.environ.get("BROWSER_USE_MCP_PYTHON") or "").strip()
	assert python_bin, "Set BROWSER_USE_MCP_PYTHON for tests"

	with _start_unified(repo_root=repo_root, python_bin=python_bin, session_id="unified-internal-errors") as (unified, _state_dir):
		# Context7 errors should be deterministic when API key isn't configured.
		c7_resolve = unified.request(
			"tools/call",
			{
				"name": "context7_resolve_library_id",
				"arguments": {"libraryName": "react", "query": "hooks"},
			},
			timeout_s=45.0,
		)
		assert "CONTEXT7_API_KEY is not set" in tool_text(c7_resolve)

		c7_query = unified.request(
			"tools/call",
			{
				"name": "context7_query_docs",
				"arguments": {"libraryId": "/vercel/next.js", "query": "app router", "tokens": 200},
			},
			timeout_s=45.0,
		)
		assert "CONTEXT7_API_KEY is not set" in tool_text(c7_query)

		# docker_vm_run argument validation
		missing_cmd = unified.request("tools/call", {"name": "docker_vm_run", "arguments": {}}, timeout_s=30.0)
		assert "required property" in tool_text(missing_cmd) and "command" in tool_text(missing_cmd)

		both_repo = unified.request(
			"tools/call",
			{
				"name": "docker_vm_run",
				"arguments": {"command": "echo ok", "repo_path": str(repo_root), "repo_url": "https://example.com/repo.git"},
			},
			timeout_s=30.0,
		)
		assert "Error: provide only one of repo_path or repo_url" in tool_text(both_repo)

		# Agent S3 VM argument validation
		missing_task = unified.request("tools/call", {"name": "agent_s3_vm_run_task", "arguments": {}}, timeout_s=30.0)
		assert "required property" in tool_text(missing_task) and "task" in tool_text(missing_task)

		vm_both_repo = unified.request(
			"tools/call",
			{
				"name": "agent_s3_vm_selftest",
				"arguments": {"repo_path": str(repo_root), "repo_url": "https://example.com/repo.git", "timeout_s": 60},
			},
			timeout_s=120.0,
		)
		assert "Provide only one of repo_path or repo_url" in tool_text(vm_both_repo)

		unknown = unified.request("tools/call", {"name": "does-not-exist", "arguments": {}}, timeout_s=30.0)
		assert "Error: Unknown tool:" in tool_text(unknown)


def test_unified_disable_ui_describe(_: Harness) -> None:
	repo_root = Path(__file__).resolve().parents[1]
	python_bin = (os.environ.get("BROWSER_USE_MCP_PYTHON") or "").strip()
	assert python_bin, "Set BROWSER_USE_MCP_PYTHON for tests"

	with _start_unified(
		repo_root=repo_root,
		python_bin=python_bin,
		session_id="unified-disable-ui-describe",
		extra_env={"MCP_PLUS_ENABLE_UI_DESCRIBE": "false"},
	) as (unified, _state_dir):
		resp = unified.request("tools/list", {}, timeout_s=45.0)
		tools = (resp.get("result") or {}).get("tools") or []
		names = {t.get("name") for t in tools if isinstance(t, dict)}

		assert "browser-use.browser_navigate" in names
		assert "chrome-devtools.evaluate_script" in names
		assert "ui-describe.ui_describe" not in names

		call = unified.request(
			"tools/call",
			{"name": "ui-describe.ui_describe", "arguments": {"url_contains": "example", "selector": "body"}},
			timeout_s=30.0,
		)
		assert "Unknown tool" in tool_text(call)


def test_unified_docker_vm_files_and_repo_mount(_: Harness) -> None:
	repo_root = Path(__file__).resolve().parents[1]
	python_bin = (os.environ.get("BROWSER_USE_MCP_PYTHON") or "").strip()
	assert python_bin, "Set BROWSER_USE_MCP_PYTHON for tests"

	payload = base64.b64encode(b"hello-from-input\n").decode("ascii")

	with _start_unified(repo_root=repo_root, python_bin=python_bin, session_id="unified-docker-files") as (unified, _state_dir):
		resp = unified.request(
			"tools/call",
			{
				"name": "docker_vm_run",
				"arguments": {
					"image": "alpine:3.19",
					"repo_path": str(repo_root),
					"files": [{"path": "hello.txt", "content_b64": payload}],
					"command": "cat /workspace/input/hello.txt && test -f /workspace/repo/README.md",
					"timeout_s": 120,
				},
			},
			timeout_s=180.0,
		)
		out = tool_json(resp)
		assert out.get("exit_code") == 0, out
		assert "hello-from-input" in (out.get("stdout") or "")


def _find_free_port() -> int:
	sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	sock.bind(("127.0.0.1", 0))
	port = int(sock.getsockname()[1])
	sock.close()
	return port


@contextlib.contextmanager
def _start_git_daemon() -> contextlib.Iterator[str]:
	with tempfile.TemporaryDirectory(prefix="mcp-plus-git-daemon-") as tmp:
		tmp_path = Path(tmp)
		src = tmp_path / "src"
		bare = tmp_path / "repo.git"
		src.mkdir(parents=True, exist_ok=True)
		bare.parent.mkdir(parents=True, exist_ok=True)

		subprocess.run(["git", "init"], check=True, cwd=str(src), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		(src / "README.md").write_text("hello\n", encoding="utf-8")
		subprocess.run(["git", "add", "README.md"], check=True, cwd=str(src), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		subprocess.run(
			["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
			check=True,
			cwd=str(src),
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)

		subprocess.run(
			["git", "init", "--bare", str(bare)], check=True, cwd=str(tmp_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
		)
		subprocess.run(["git", "-C", str(src), "branch", "-M", "main"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		subprocess.run(
			["git", "remote", "add", "origin", str(bare)], check=True, cwd=str(src), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
		)
		subprocess.run(
			["git", "push", "-u", "origin", "main"], check=True, cwd=str(src), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
		)
		subprocess.run(
			["git", "-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"],
			check=True,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)

		port = _find_free_port()
		url = f"git://127.0.0.1:{port}/repo.git"

		proc = subprocess.Popen(
			[
				"git",
				"daemon",
				"--reuseaddr",
				"--export-all",
				f"--base-path={tmp_path}",
				f"--port={port}",
				"--listen=127.0.0.1",
				str(tmp_path),
			],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			text=True,
		)
		try:
			for _ in range(50):
				if proc.poll() is not None:
					raise RuntimeError("git daemon exited early")
				try:
					subprocess.run(["git", "ls-remote", url], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
					break
				except Exception:
					time.sleep(0.05)
			else:
				raise RuntimeError("git daemon not ready")
			yield url
		finally:
			proc.terminate()
			try:
				proc.wait(timeout=2.0)
			except Exception:
				proc.kill()


def test_unified_agent_s3_repo_url_clone(_: Harness) -> None:
	repo_root = Path(__file__).resolve().parents[1]
	python_bin = (os.environ.get("BROWSER_USE_MCP_PYTHON") or "").strip()
	assert python_bin, "Set BROWSER_USE_MCP_PYTHON for tests"

	with _start_git_daemon() as repo_url:
		with _start_unified(repo_root=repo_root, python_bin=python_bin, session_id="unified-agent-s3-clone") as (
			unified,
			_state_dir,
		):
			vm = unified.request(
				"tools/call",
				{
					"name": "agent_s3_vm_selftest",
					"arguments": {"repo_url": repo_url, "host_network": True, "timeout_s": 900},
				},
				timeout_s=900.0,
			)
			vm_out = tool_json(vm)
			assert vm_out.get("run", {}).get("exit_code") == 0, vm_out
			assert "Cloning repo:" in (vm_out.get("run", {}).get("stderr") or "")

			vm_json = json.loads((vm_out.get("run", {}).get("stdout") or "").strip())
			assert vm_json.get("ok") is True, vm_json
			repo_probe = next((r for r in (vm_json.get("results") or []) if r.get("name") == "repo_dir"), None)
			assert repo_probe and repo_probe.get("ok") is True, vm_json
			assert (repo_probe.get("value") or {}).get("entries", 0) > 0
