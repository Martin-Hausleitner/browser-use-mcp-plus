from __future__ import annotations

import os
import subprocess
from pathlib import Path


def repo_root() -> Path:
	# servers/ is one directory below repo root.
	return Path(__file__).resolve().parents[1]


def default_state_root() -> Path:
	explicit = (os.getenv("BROWSER_USE_MCP_STATE_DIR") or "").strip()
	if explicit:
		return Path(explicit).expanduser()
	xdg = (os.getenv("XDG_STATE_HOME") or "").strip()
	if xdg:
		return Path(xdg).expanduser() / "browser-use-mcp-plus"
	return Path("~/.local/state/browser-use-mcp-plus").expanduser()


def shared_state_path() -> Path:
	explicit = (os.getenv("BROWSER_USE_MCP_SHARED_STATE_PATH") or "").strip()
	if explicit:
		return Path(explicit).expanduser()
	return default_state_root() / "shared_state.json"


def ensure_cdp_chrome_ready(*, timeout_s: int = 45) -> None:
	explicit = (os.getenv("BROWSER_USE_MCP_ENSURE_CHROME_SCRIPT") or "").strip()
	script = Path(explicit).expanduser() if explicit else (repo_root() / "bin" / "ensure_cdp_chrome.sh")
	if not script.exists():
		return
	subprocess.run(
		["bash", str(script)],
		check=True,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		timeout=timeout_s,
	)


def looks_like_cdp_connect_error(exc: Exception) -> bool:
	msg = str(exc)
	return any(token in msg for token in ("connect ECONNREFUSED", "ECONNREFUSED", "connect_over_cdp", "Failed to connect"))

