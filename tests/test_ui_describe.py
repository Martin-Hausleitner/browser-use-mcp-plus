from __future__ import annotations

from tests._harness import Harness
from tests._util import tool_text


def test_overlay_cleanup(h: Harness) -> None:
	# Create fake browser-use overlay nodes.
	inject = """
(() => {
  const root = document.createElement('div');
  root.id = 'browser-use-debug-highlights';
  root.setAttribute('data-browser-use-interaction-highlight', '1');
  document.body.appendChild(root);
  return document.querySelectorAll('[data-browser-use-interaction-highlight],[data-browser-use-coordinate-highlight],[data-browser-use-highlight],#browser-use-debug-highlights').length;
})()
""".strip()
	before = h.chrome_devtools.request(
		"tools/call",
		{"name": "evaluate_script", "arguments": {"url_contains": h.url_contains, "script": inject}},
		timeout_s=30.0,
	)
	before_text = tool_text(before)
	assert '"result"' in before_text, f"Unexpected evaluate_script output (before): {before_text!r}"

	# ui_describe should remove overlays before taking the screenshot.
	h.ui_describe.request(
		"tools/call",
		{"name": "ui_describe", "arguments": {"url_contains": h.url_contains, "max_chars": 120}},
		timeout_s=60.0,
	)

	count_after = h.chrome_devtools.request(
		"tools/call",
		{
			"name": "evaluate_script",
			"arguments": {
				"url_contains": h.url_contains,
				"script": "document.querySelectorAll('[data-browser-use-interaction-highlight],[data-browser-use-coordinate-highlight],[data-browser-use-highlight],#browser-use-debug-highlights').length",
			},
		},
		timeout_s=30.0,
	)
	text = tool_text(count_after)
	assert '"result": 0' in text, f"Expected overlays to be removed, got: {text!r}"
