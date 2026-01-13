from __future__ import annotations

import json
from typing import Any


def tool_text(resp: dict) -> str:
	return (((resp.get("result") or {}).get("content") or [{}])[0] or {}).get("text") or ""


def tool_json(resp: dict) -> Any:
	text = tool_text(resp)
	try:
		return json.loads(text)
	except Exception as exc:  # noqa: BLE001
		raise AssertionError(f"Expected JSON tool response, got: {text!r}") from exc

