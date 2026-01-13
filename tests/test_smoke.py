from __future__ import annotations

from tests._harness import Harness
from tests._util import tool_text


def test_tools_list(h: Harness) -> None:
	bu = h.browser_use.request("tools/list", {}, timeout_s=20.0)
	bu_tools = (bu.get("result") or {}).get("tools") or []
	bu_names = {t.get("name") for t in bu_tools if isinstance(t, dict)}
	assert "browser_navigate" in bu_names

	ui = h.ui_describe.request("tools/list", {}, timeout_s=20.0)
	ui_tools = (ui.get("result") or {}).get("tools") or []
	ui_names = {t.get("name") for t in ui_tools if isinstance(t, dict)}
	assert "ui_describe" in ui_names
	assert "set_browser_keep_open" in ui_names

	dt = h.chrome_devtools.request("tools/list", {}, timeout_s=20.0)
	dt_tools = (dt.get("result") or {}).get("tools") or []
	dt_names = {t.get("name") for t in dt_tools if isinstance(t, dict)}
	assert "evaluate_script" in dt_names
	assert "set_browser_keep_open" in dt_names


def test_devtools_evaluate_title(h: Harness) -> None:
	resp = h.chrome_devtools.request(
		"tools/call",
		{"name": "evaluate_script", "arguments": {"url_contains": h.url_contains, "script": "document.title"}},
		timeout_s=30.0,
	)
	text = tool_text(resp)
	assert "MCP Plus Test Fixture" in text, f"Unexpected evaluate_script output: {text!r}"
