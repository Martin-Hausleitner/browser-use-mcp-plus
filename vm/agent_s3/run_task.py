from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gui_agents.core.AgentS import GraphSearchAgent  # type: ignore

DEFAULT_BASE_URL = "https://llm.chutes.ai/v1"
DEFAULT_VISION_MODEL = "Qwen/Qwen3-VL-235B-A22B-Instruct"


def _require_api_key() -> str:
	for name in ("CHUTES_API_KEY", "OPENAI_API_KEY", "API_KEY"):
		val = (os.environ.get(name) or "").strip()
		if val and val.strip() not in {"YOUR_KEY_HERE", "YOUR_CHUTES_API_KEY_HERE"}:
			return val
	raise RuntimeError(
		"Missing API key. Set CHUTES_API_KEY (preferred) or OPENAI_API_KEY in the container env."
	)


def _load_env_file(path: str) -> None:
	with open(path, "r", encoding="utf-8") as f:
		for raw_line in f:
			line = raw_line.strip()
			if not line or line.startswith("#") or "=" not in line:
				continue
			key, value = line.split("=", 1)
			key = key.strip()
			value = value.strip().strip('"').strip("'")
			if key not in os.environ:
				os.environ[key] = value


def _observe(UIElement: Any) -> Dict[str, Any]:
	import pyautogui  # type: ignore

	screenshot = pyautogui.screenshot()
	buf = io.BytesIO()
	screenshot.save(buf, format="PNG")
	return {"screenshot": buf.getvalue(), "accessibility_tree": UIElement.systemWideElement()}


def _exec_action(code: str, *, unsafe_exec: bool) -> None:
	if unsafe_exec:
		exec(code, {})
		return

	allowed_imports = {"pyautogui", "time", "subprocess", "difflib", "os", "pathlib", "shlex"}
	builtins_import = __import__

	def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
		top_level = name.split(".", 1)[0]
		if top_level not in allowed_imports:
			raise ImportError(f"Blocked import: {name}")
		return builtins_import(name, globals, locals, fromlist, level)

	safe_builtins = {"__import__": restricted_import}
	safe_globals = {"__builtins__": safe_builtins}
	for module_name in sorted(allowed_imports):
		try:
			safe_globals[module_name] = builtins_import(module_name)
		except Exception:
			pass

	# Guardrail: prevent excessive click spam unless user opts into unsafe mode.
	max_clicks_raw = os.environ.get("AGENT_S3_MAX_CLICKS", "3")
	try:
		max_clicks = int(max_clicks_raw)
	except ValueError:
		raise RuntimeError(f"Invalid AGENT_S3_MAX_CLICKS={max_clicks_raw!r}")
	match = re.search(r"pyautogui\.click\([^)]*?clicks\s*=\s*(\d+)", code)
	if match and int(match.group(1)) > max_clicks:
		raise RuntimeError(
			f"Refusing pyautogui.click() with clicks={match.group(1)} (limit {max_clicks}). "
			"Set --unsafe-exec or raise AGENT_S3_MAX_CLICKS."
		)

	exec(code, safe_globals, {})


def _make_agent() -> Tuple[GraphSearchAgent, Any, Dict[str, Any]]:
	from gui_agents.aci.LinuxOSACI import LinuxACI, UIElement  # type: ignore

	grounding_agent = LinuxACI(ocr=False)
	engine_params = {
		"engine_type": os.environ.get("ENGINE_TYPE", "vllm"),
		"model": os.environ.get("VISION_MODEL", DEFAULT_VISION_MODEL),
		"base_url": os.environ.get("BASE_URL", DEFAULT_BASE_URL),
		"api_key": _require_api_key(),
	}
	agent = GraphSearchAgent(
		engine_params=engine_params,
		grounding_agent=grounding_agent,
		platform="ubuntu",
		action_space="pyautogui",
		observation_type="mixed",
	)
	return agent, UIElement, engine_params


def run_task_with_trace(
	instruction: str,
	*,
	max_steps: int,
	dry_run: bool,
	unsafe_exec: bool,
	sleep_after_exec_s: float,
	include_screenshot_b64: bool,
) -> Dict[str, Any]:
	agent, UIElement, engine_params = _make_agent()

	trace: List[Dict[str, Any]] = []
	actions: List[str] = []

	for step_index in range(max_steps):
		obs = _observe(UIElement)
		if include_screenshot_b64:
			obs = dict(obs)
			obs["screenshot_b64"] = base64.b64encode(obs["screenshot"]).decode("ascii")
			obs.pop("screenshot", None)
		info, predicted = agent.predict(instruction=instruction, observation=obs)
		if not predicted:
			raise RuntimeError("No actions returned by Agent S3.")
		action = predicted[0]
		if not isinstance(action, str):
			raise TypeError(f"Unexpected action type: {type(action)}")
		actions.append(action)

		event: Dict[str, Any] = {
			"step": step_index,
			"subtask": info.get("subtask"),
			"subtask_status": info.get("subtask_status"),
			"plan_code": info.get("plan_code"),
			"action": action,
		}

		lower = action.strip().lower()
		if "done" in lower or "fail" in lower:
			event["executed"] = False
			trace.append(event)
			return {"engine_params": engine_params, "final_info": info, "actions": actions, "trace": trace}

		if "next" in lower or "wait" in lower:
			event["executed"] = False
			trace.append(event)
			if "wait" in lower:
				time.sleep(5)
			continue

		if dry_run:
			event["executed"] = False
			event["dry_run"] = True
			trace.append(event)
			continue

		try:
			_exec_action(action, unsafe_exec=unsafe_exec)
		except Exception as exc:
			event["executed"] = False
			event["exec_error"] = f"{type(exc).__name__}: {exc}"
			trace.append(event)
			return {
				"engine_params": engine_params,
				"final_info": info,
				"actions": actions,
				"trace": trace,
				"error": {"phase": "exec", "exception_type": type(exc).__name__, "exception": str(exc)},
			}
		else:
			event["executed"] = True
			trace.append(event)
		time.sleep(sleep_after_exec_s)

	return {"engine_params": engine_params, "final_info": {"subtask_status": "max_steps"}, "actions": actions, "trace": trace}


def main(argv: List[str]) -> int:
	parser = argparse.ArgumentParser(description="Run Agent S3 (gui-agents) inside the VM container.")
	parser.add_argument("--query", type=str, required=True, help="Task instruction.")
	parser.add_argument("--steps", type=int, default=15, help="Max agent steps.")
	parser.add_argument("--env-file", type=str, default=None, help="Optional env file path.")
	parser.add_argument("--workdir", type=str, default=None, help="Working directory (e.g. /workspace/repo).")
	parser.add_argument("--dry-run", action="store_true", help="Do not execute actions; only generate trace.")
	parser.add_argument("--unsafe-exec", action="store_true", help="Run actions with unrestricted exec().")
	parser.add_argument("--sleep-after-exec-s", type=float, default=1.0)
	parser.add_argument("--include-screenshot-b64", action="store_true", help="Embed screenshot in output JSON (large).")
	args = parser.parse_args(argv)

	if args.env_file:
		_load_env_file(args.env_file)

	if args.workdir:
		Path(args.workdir).mkdir(parents=True, exist_ok=True)
		os.chdir(args.workdir)

	out = run_task_with_trace(
		args.query,
		max_steps=args.steps,
		dry_run=bool(args.dry_run),
		unsafe_exec=bool(args.unsafe_exec),
		sleep_after_exec_s=float(args.sleep_after_exec_s),
		include_screenshot_b64=bool(args.include_screenshot_b64),
	)
	print(json.dumps(out, ensure_ascii=False, indent=2))
	return 0 if not out.get("error") else 1


if __name__ == "__main__":
	raise SystemExit(main(list(__import__("sys").argv[1:])))
