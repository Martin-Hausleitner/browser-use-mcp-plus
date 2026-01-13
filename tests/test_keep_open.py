from __future__ import annotations

from pathlib import Path

from tests._harness import Harness
from tests._util import tool_json


def test_set_browser_keep_open_ui_describe(h: Harness) -> None:
	resp = h.ui_describe.request(
		"tools/call",
		{"name": "set_browser_keep_open", "arguments": {"keep_open": True}},
		timeout_s=30.0,
	)
	obj = tool_json(resp)
	marker = Path(obj["marker_file"])
	assert marker.exists()

	resp2 = h.ui_describe.request(
		"tools/call",
		{"name": "set_browser_keep_open", "arguments": {"keep_open": False}},
		timeout_s=30.0,
	)
	obj2 = tool_json(resp2)
	marker2 = Path(obj2["marker_file"])
	assert not marker2.exists()
