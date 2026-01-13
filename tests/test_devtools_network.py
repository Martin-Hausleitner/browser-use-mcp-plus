from __future__ import annotations

import json
import time

from tests._harness import Harness
from tests._util import tool_json


def test_network_capture_fetch(h: Harness) -> None:
	path = f"/ping.txt?ts={int(time.time() * 1000)}"
	script = f"(() => fetch({json.dumps(path)}).then(r => r.text()).then(t => t.trim()))()"

	resp = h.chrome_devtools.request(
		"tools/call",
		{"name": "evaluate_script", "arguments": {"url_contains": h.url_contains, "script": script}},
		timeout_s=30.0,
	)
	obj = tool_json(resp)
	assert str(obj.get("result") or "").strip() == "pong", f"Unexpected fetch result: {obj!r}"

	req_key: str | None = None
	last: dict | None = None
	for _ in range(30):
		r = h.chrome_devtools.request(
			"tools/call",
			{"name": "list_network_requests", "arguments": {"url_contains": h.url_contains, "limit": 400}},
			timeout_s=30.0,
		)
		lst = tool_json(r)
		last = lst if isinstance(lst, dict) else None
		reqs = (lst.get("requests") or []) if isinstance(lst, dict) else []
		for item in reqs:
			if isinstance(item, dict) and "ping.txt" in str(item.get("url") or ""):
				req_key = str(item.get("id") or "")
				break
		if req_key:
			break
		time.sleep(0.1)

	assert req_key, f"Expected ping request not found. last={last!r}"

	r2 = h.chrome_devtools.request(
		"tools/call",
		{"name": "get_network_request", "arguments": {"request_id": req_key, "include_response_body": False}},
		timeout_s=30.0,
	)
	obj2 = tool_json(r2)
	assert "ping.txt" in str(obj2.get("url") or ""), f"Unexpected get_network_request output: {obj2!r}"

