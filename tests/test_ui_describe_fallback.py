from __future__ import annotations

from tests._harness import Harness
from tests._util import tool_text


def test_ui_describe_without_llm_returns_note(h: Harness) -> None:
	resp = h.ui_describe.request(
		"tools/call",
		{"name": "ui_describe", "arguments": {"url_contains": h.url_contains, "max_chars": 200}},
		timeout_s=60.0,
	)
	text = tool_text(resp)
	assert "LLM not configured" in text, f"Unexpected ui_describe output: {text!r}"

