from __future__ import annotations

import contextlib
import os
import signal
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from mcp_plus.fixture_server import serve_static_dir
from mcp_plus.stdio_client import MCPStdioClient


@dataclass(frozen=True)
class Harness:
	repo_root: Path
	state_dir: Path
	profile_dir: Path
	session_id: str
	url: str
	url_contains: str
	browser_use: MCPStdioClient
	ui_describe: MCPStdioClient
	chrome_devtools: MCPStdioClient


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
	(root / "ping.txt").write_text("pong\n", encoding="utf-8")
	return root


def _require_env(name: str) -> str:
	val = (os.getenv(name) or "").strip()
	if not val:
		raise RuntimeError(f"Missing env var: {name}")
	return val


def _sanitize_session_id(raw: str) -> str:
	keep = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@+-"
	safe = "".join((ch if ch in keep else "_") for ch in (raw or ""))
	safe = safe.strip("_") or "session"
	return safe[:80]


def _read_pid_file(path: Path) -> int | None:
	try:
		raw = path.read_text(encoding="utf-8", errors="ignore")
	except Exception:
		return None
	digits = "".join(ch for ch in raw if ch.isdigit())
	if not digits:
		return None
	try:
		pid = int(digits)
	except Exception:
		return None
	return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
	try:
		os.kill(pid, 0)
		return True
	except Exception:
		return False


def _kill_pid(pid: int) -> None:
	try:
		os.kill(pid, signal.SIGTERM)
	except Exception:
		return
	for _ in range(40):
		if not _pid_alive(pid):
			return
		time.sleep(0.05)
	try:
		os.kill(pid, signal.SIGKILL)
	except Exception:
		return


def _cleanup_session_processes(*, state_dir: Path, session_id: str) -> None:
	session_dir = state_dir / "sessions" / _sanitize_session_id(session_id)
	reaper_pid = _read_pid_file(session_dir / "chrome.reaper.pid")
	chrome_pid = _read_pid_file(session_dir / "chrome.pid")

	# Kill reaper first so it doesn't keep Chrome around (tests toggle keep-open).
	if reaper_pid:
		_kill_pid(reaper_pid)
	if chrome_pid:
		_kill_pid(chrome_pid)


@contextlib.contextmanager
def start_harness(*, session_id: str = "test-suite") -> Iterator[Harness]:
	repo_root = Path(__file__).resolve().parents[1]
	python_bin = (os.getenv("BROWSER_USE_MCP_PYTHON") or "").strip()
	if not python_bin:
		raise RuntimeError(
			"Set BROWSER_USE_MCP_PYTHON to a python that has the required deps installed "
			"(browser_use, mcp, playwright, ...)."
		)

	with tempfile.TemporaryDirectory(prefix="browser-use-mcp-plus-tests-") as tmp:
		tmp_path = Path(tmp)
		state_dir = tmp_path / "state"
		profile_dir = tmp_path / "profiles"
		state_dir.mkdir(parents=True, exist_ok=True)
		profile_dir.mkdir(parents=True, exist_ok=True)

		fixture_root = _make_fixture_dir(tmp_path)

		common_env = {
			"BROWSER_USE_MCP_PYTHON": python_bin,
			"BROWSER_USE_SESSION_ID": session_id,
			"BROWSER_USE_CHROME_MODE": os.getenv("BROWSER_USE_CHROME_MODE", "session"),
			"BROWSER_USE_ALLOW_HEADLESS_FALLBACK": os.getenv("BROWSER_USE_ALLOW_HEADLESS_FALLBACK", "true"),
			"BROWSER_USE_MCP_STATE_DIR": str(state_dir),
			"BROWSER_USE_CDP_PROFILE_BASE_DIR": str(profile_dir),
			# Keep tests offline/deterministic (ui-describe should fall back to the "LLM not configured" note).
			"OPENAI_API_KEY": "",
			"OPENAI_API_BASE": "",
			"OPENAI_BASE_URL": "",
		}

		with serve_static_dir(fixture_root) as (url, url_contains):
			browser_use = MCPStdioClient(
				name="browser-use",
				command=[str(repo_root / "bin" / "browser_use_mcp.sh")],
				env=common_env,
				cwd=str(repo_root),
			)
			ui = MCPStdioClient(
				name="ui-describe",
				command=[str(repo_root / "bin" / "ui_describe_mcp.sh")],
				env=common_env,
				cwd=str(repo_root),
			)
			devtools = MCPStdioClient(
				name="chrome-devtools",
				command=[str(repo_root / "bin" / "chrome_devtools_mcp.sh")],
				env=common_env,
				cwd=str(repo_root),
			)

			browser_use.start()
			ui.start()
			devtools.start()

			try:
				browser_use.initialize()
				ui.initialize()
				devtools.initialize()

				# Navigate once so all tests have a stable tab.
				browser_use.request(
					"tools/call",
					{"name": "browser_navigate", "arguments": {"url": url}},
					timeout_s=45.0,
				)

				yield Harness(
					repo_root=repo_root,
					state_dir=state_dir,
					profile_dir=profile_dir,
					session_id=session_id,
					url=url,
					url_contains=url_contains,
					browser_use=browser_use,
					ui_describe=ui,
					chrome_devtools=devtools,
				)
			finally:
				with contextlib.suppress(Exception):
					devtools.close()
				with contextlib.suppress(Exception):
					ui.close()
				with contextlib.suppress(Exception):
					browser_use.close()
				with contextlib.suppress(Exception):
					_cleanup_session_processes(state_dir=state_dir, session_id=session_id)
