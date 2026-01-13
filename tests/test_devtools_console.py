from __future__ import annotations

import json
import time

from tests._harness import Harness
from tests._util import tool_json


def test_console_log_capture(h: Harness) -> None:
	marker = f"mcp-plus-console-test-{int(time.time() * 1000)}"
	script = f"(() => {{ console.log({json.dumps(marker)}); return true; }})()"

	h.chrome_devtools.request(
		"tools/call",
		{"name": "evaluate_script", "arguments": {"url_contains": h.url_contains, "script": script}},
		timeout_s=30.0,
	)

	found = False
	last: dict | None = None
	for _ in range(30):
		resp = h.chrome_devtools.request(
			"tools/call",
			{"name": "list_console_messages", "arguments": {"url_contains": h.url_contains, "limit": 200}},
			timeout_s=30.0,
		)
		obj = tool_json(resp)
		last = obj if isinstance(obj, dict) else None
		msgs = (obj.get("messages") or []) if isinstance(obj, dict) else []
		for m in msgs:
			if isinstance(m, dict) and marker in str(m.get("text") or ""):
				found = True
				break
		if found:
			break
		time.sleep(0.1)

	assert found, f"Expected console message not found. marker={marker!r} last={last!r}"

