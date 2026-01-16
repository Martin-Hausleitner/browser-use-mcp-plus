from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path


def _ok(name: str, value: object) -> dict:
	return {"name": name, "ok": True, "value": value}


def _fail(name: str, exc: Exception) -> dict:
	return {"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
	results: list[dict] = []

	results.append(_ok("python", sys.version.split()[0]))
	results.append(_ok("platform", platform.platform()))

	try:
		out = subprocess.run(["git", "--version"], check=True, stdout=subprocess.PIPE, text=True).stdout.strip()
		results.append(_ok("git", out))
	except Exception as exc:
		results.append(_fail("git", exc))

	try:
		import gui_agents  # type: ignore

		results.append(_ok("gui_agents_import", getattr(gui_agents, "__version__", "unknown")))
	except Exception as exc:
		results.append(_fail("gui_agents_import", exc))

	try:
		import pyautogui  # type: ignore

		img = pyautogui.screenshot()
		results.append(_ok("pyautogui_screenshot", {"size": list(getattr(img, "size", (0, 0))) }))
	except Exception as exc:
		results.append(_fail("pyautogui_screenshot", exc))

	try:
		from gui_agents.aci.LinuxOSACI import LinuxACI, UIElement  # type: ignore

		_ = LinuxACI(ocr=False)
		root = UIElement.systemWideElement()
		# Some objects can be huge; stringify minimally.
		results.append(_ok("accessibility_tree", {"type": type(root).__name__}))
	except Exception as exc:
		results.append(_fail("accessibility_tree", exc))

	repo_dir = Path(os.getenv("VM_REPO_DIR", "/workspace/repo"))
	try:
		exists = repo_dir.exists()
		count = len(list(repo_dir.iterdir())) if exists else 0
		results.append(_ok("repo_dir", {"path": str(repo_dir), "exists": exists, "entries": count}))
	except Exception as exc:
		results.append(_fail("repo_dir", exc))

	ok = all(r.get("ok") for r in results)
	print(json.dumps({"ok": ok, "results": results}, ensure_ascii=False, indent=2))
	return 0 if ok else 1


if __name__ == "__main__":
	raise SystemExit(main())
