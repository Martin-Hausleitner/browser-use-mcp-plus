from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp_plus.fixture_server import serve_static_dir
from mcp_plus.stdio_client import MCPStdioClient

# Reuse the proven helpers from the single-scenario runner.
from scripts.live_llm_e2e import (  # noqa: PLC2701
	_cleanup_session_processes,
	_extract_eval_result,
	_get_context7_api_key,
	_get_openai_api_key,
	_openai_chat,
	_require_env,
	_resolve_openai_base_url,
	_safe_join,
	_sanitize_session_id,
	_tool_text,
)


def _poll_eval(
	unified: MCPStdioClient,
	*,
	url_contains: str,
	script: str,
	timeout_s: float = 3.0,
	interval_s: float = 0.12,
	predicate: Any | None = None,
) -> dict[str, Any]:
	deadline = time.time() + max(0.1, float(timeout_s))
	last: dict[str, Any] | None = None
	while time.time() < deadline:
		resp = unified.request(
			"tools/call",
			{"name": "chrome-devtools.evaluate_script", "arguments": {"url_contains": url_contains, "script": script}},
			timeout_s=45.0,
		)
		last = _extract_eval_result(_tool_text(resp))
		if predicate is None:
			return last
		try:
			if predicate(last):
				return last
		except Exception:
			pass
		time.sleep(float(interval_s))
	return last or {}


def _poll_network_ping_ok(
	unified: MCPStdioClient,
	*,
	url_contains: str,
	timeout_s: float = 3.0,
	interval_s: float = 0.15,
) -> tuple[bool, dict[str, Any] | None]:
	deadline = time.time() + max(0.1, float(timeout_s))
	last_obj: dict[str, Any] | None = None
	while time.time() < deadline:
		net = unified.request(
			"tools/call",
			{
				"name": "chrome-devtools.list_network_requests",
				"arguments": {"url_contains": url_contains, "limit": 200, "include_headers": False},
			},
			timeout_s=45.0,
		)
		obj = _tool_json(net)
		last_obj = obj if isinstance(obj, dict) else None
		reqs = (last_obj or {}).get("requests")
		if isinstance(reqs, list):
			for r in reversed(reqs):
				if not isinstance(r, dict):
					continue
				url_str = str(r.get("url") or "")
				if "/ping.txt" not in url_str:
					continue
				status = r.get("status")
				if isinstance(status, int) and status == 200:
					return True, last_obj
		time.sleep(float(interval_s))
	return False, last_obj


def _openai_list_models(*, api_key: str, base_url: str, timeout_s: float = 20.0, max_retries: int = 1) -> list[str]:
	url = f"{base_url.rstrip('/')}/models"
	headers = {"Authorization": f"Bearer {api_key}"}

	last_err: Exception | None = None
	for attempt in range(max_retries + 1):
		try:
			req = urllib.request.Request(url, method="GET", headers=headers)
			with urllib.request.urlopen(req, timeout=timeout_s) as resp:
				raw = resp.read().decode("utf-8", errors="replace")
			obj = json.loads(raw)
			data = obj.get("data") if isinstance(obj, dict) else None
			out: list[str] = []
			if isinstance(data, list):
				for entry in data:
					if not isinstance(entry, dict):
						continue
					val = entry.get("id") or entry.get("name")
					if isinstance(val, str) and val.strip():
						out.append(val.strip())
			return sorted(set(out))
		except urllib.error.HTTPError as exc:
			raw = exc.read().decode("utf-8", errors="replace") if getattr(exc, "fp", None) else str(exc)
			last_err = RuntimeError(f"OpenAI HTTPError {exc.code}: {raw[:2000]}")
		except Exception as exc:  # noqa: BLE001
			last_err = exc
		if attempt < max_retries:
			time.sleep(0.4 * (2**attempt) + random.random() * 0.2)
			continue
		if last_err is None:
			raise RuntimeError("OpenAI models request failed (unknown error)")
		raise RuntimeError(f"OpenAI models request failed: {type(last_err).__name__}: {last_err}") from last_err


def _pick_model(*, requested: str, models: list[str]) -> str:
	req = (requested or "").strip()
	if req and req.lower() != "auto":
		return req

	# Prefer Gemini 3 Pro variants when available.
	priorities = [
		"gemini-3-pro-preview",
		"gemini-3-pro",
		"gemini-3.0-pro",
	]
	lower = {m.lower(): m for m in models}
	for p in priorities:
		if p in lower:
			return lower[p]

	# Fallback: best-effort partial match.
	for m in models:
		if "gemini" in m.lower() and "pro" in m.lower() and "3" in m:
			return m
	return models[0] if models else req or "gpt-4o-mini"


def _tool_json(resp: dict) -> Any:
	text = _tool_text(resp)
	try:
		return json.loads(text)
	except Exception as exc:  # noqa: BLE001
		raise RuntimeError(f"Expected JSON tool response, got: {text[:400]!r}") from exc


def _write_fixture_console_error(site: Path) -> dict[str, Path]:
	site.mkdir(parents=True, exist_ok=True)
	index = site / "index.html"
	index.write_text(
		textwrap.dedent(
			"""\
			<!doctype html>
			<html lang="de">
			  <head>
			    <meta charset="utf-8" />
			    <meta name="viewport" content="width=device-width, initial-scale=1" />
			    <title>MCP Plus Live Console Lab</title>
			    <style>
			      body { margin: 0; font-family: system-ui, sans-serif; background: #0f1115; color: #e9ecf1; }
			      .wrap { max-width: 860px; margin: 0 auto; padding: 22px 18px; }
			      .card { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06); border-radius: 14px; padding: 18px; }
			      #status { font-weight: 600; }
			    </style>
			  </head>
			  <body>
			    <div class="wrap">
			      <div class="card">
			        <h1>Console Lab</h1>
			        <p>Diese Seite erzeugt absichtlich einen JS-Fehler im Console-Log. Der Live-Test soll ihn finden und fixen.</p>
			        <p>Status: <span id="status">starting</span></p>
			      </div>
			    </div>
			    <script>
			      // Intentionally broken: throws an exception on load.
			      window.__app_ok = false;
			      function boot() {
			        document.getElementById('status').textContent = 'booting';
			        doesNotExist(); // <- fix me
			      }
			      boot();
			    </script>
			  </body>
			</html>
			"""
		),
		encoding="utf-8",
	)
	return {"index": index}


CONSOLE_METRICS_SCRIPT = r"""
(() => {
  const status = document.querySelector('#status');
  return {
    okFlag: (window.__app_ok === true),
    statusText: status ? String(status.textContent || '') : null,
    title: document.title,
    href: location.href,
  };
})()
"""


def run_live_console_fix(
	*,
	model: str,
	max_iters: int,
	openai_timeout_s: float,
	openai_retries: int,
) -> dict[str, Any]:
	repo_root = Path(__file__).resolve().parents[1]
	python_bin = _require_env("BROWSER_USE_MCP_PYTHON")

	openai_key = _get_openai_api_key()
	openai_base = _resolve_openai_base_url()
	context7_key = _get_context7_api_key()

	session_id = f"live-llm-console-{int(time.time())}"

	with tempfile.TemporaryDirectory(prefix="browser-use-mcp-plus-live-console-") as tmp:
		tmp_path = Path(tmp)
		state_dir = tmp_path / "state"
		profile_dir = tmp_path / "profiles"
		site_dir = tmp_path / "site"
		state_dir.mkdir(parents=True, exist_ok=True)
		profile_dir.mkdir(parents=True, exist_ok=True)

		_write_fixture_console_error(site_dir)

		common_env = {
			"BROWSER_USE_MCP_PYTHON": python_bin,
			"BROWSER_USE_SESSION_ID": session_id,
			"BROWSER_USE_CHROME_MODE": "session",
			"BROWSER_USE_ALLOW_HEADLESS_FALLBACK": "true",
			"BROWSER_USE_MCP_STATE_DIR": str(state_dir),
			"BROWSER_USE_CDP_PROFILE_BASE_DIR": str(profile_dir),
			"OPENAI_API_KEY": openai_key,
			"OPENAI_BASE_URL": openai_base,
			"CONTEXT7_API_KEY": context7_key,
			"UI_VISION_MODEL": (os.getenv("UI_VISION_MODEL") or model),
		}

		with serve_static_dir(site_dir) as (url, url_contains):
			unified = MCPStdioClient(
				name="mcp-plus",
				command=[str(repo_root / "bin" / "unified_mcp.sh")],
				env=common_env,
				cwd=str(repo_root),
			)
			unified.start()
			try:
				unified.initialize()

				tools_resp = unified.request("tools/list", {}, timeout_s=45.0)
				tools = (tools_resp.get("result") or {}).get("tools") or []
				names = {t.get("name") for t in tools if isinstance(t, dict)}
				required_tools = {
					"browser-use.browser_navigate",
					"ui-describe.ui_describe",
					"chrome-devtools.evaluate_script",
					"chrome-devtools.list_console_messages",
					"context7_resolve_library_id",
					"context7_query_docs",
				}
				missing = sorted(required_tools - names)
				if missing:
					raise RuntimeError(f"Unified server missing tools: {missing}")

				# Baseline: navigate + metrics
				baseline_url = f"{url}?v={int(time.time())}"
				unified.request(
					"tools/call",
					{"name": "browser-use.browser_navigate", "arguments": {"url": baseline_url}},
					timeout_s=60.0,
				)
				before_eval = unified.request(
					"tools/call",
					{"name": "chrome-devtools.evaluate_script", "arguments": {"url_contains": url_contains, "script": CONSOLE_METRICS_SCRIPT}},
					timeout_s=45.0,
				)
				before_metrics = _extract_eval_result(_tool_text(before_eval))

				tool_calls_trace: list[dict[str, Any]] = []
				ui_describe_used_llm: bool | None = None

				tools_for_llm = [
					{
						"type": "function",
						"function": {
							"name": "mcp_tool_call",
							"description": "Call a tool exposed by the unified MCP server (by exact tool name).",
							"parameters": {
								"type": "object",
								"properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
								"required": ["name", "arguments"],
							},
						},
					},
					{
						"type": "function",
						"function": {
							"name": "read_file",
							"description": "Read a fixture file under the provided fixture root.",
							"parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
						},
					},
					{
						"type": "function",
						"function": {
							"name": "write_file",
							"description": "Write a fixture file under the provided fixture root.",
							"parameters": {
								"type": "object",
								"properties": {"path": {"type": "string"}, "content": {"type": "string"}},
								"required": ["path", "content"],
							},
						},
					},
				]

				system = (
					"Du bist ein QA+Fix Agent. Ziel: einen echten JS-Fehler finden und beheben.\n\n"
					"Nutze mindestens einmal:\n"
					"- context7_resolve_library_id + context7_query_docs (kurz, z.B. Playwright console errors)\n"
					"- browser-use.browser_navigate\n"
					"- chrome-devtools.list_console_messages\n"
					"- ui-describe.ui_describe (Screenshot prüfen)\n"
					"- chrome-devtools.evaluate_script\n"
					"- write_file (index.html fixen)\n\n"
					"Erwartung nach Fix:\n"
					"- Kein neuer 'exception' oder 'error' Console-Eintrag nach Reload\n"
					"- window.__app_ok === true\n"
					"- #status zeigt 'OK'\n"
					"Du darfst nur in index.html schreiben."
				)
				user = (
					"URL: {url}\n"
					"url_contains: {url_contains}\n\n"
					"Vorgehen:\n"
					"1) Nutze chrome-devtools.list_console_messages um den Fehler zu sehen.\n"
					"2) Optional: ui-describe um visuell zu verifizieren.\n"
					"3) Fixe index.html so, dass boot() keinen Fehler wirft und Status/Flag gesetzt werden.\n"
					"4) Verifiziere per chrome-devtools.evaluate_script (CONSOLE_METRICS_SCRIPT) + list_console_messages.\n"
				).format(url=baseline_url, url_contains=url_contains)

				messages: list[dict[str, Any]] = [
					{"role": "system", "content": system},
					{"role": "user", "content": user},
				]

				def _run_mcp_tool(tool_name: str, args: dict[str, Any]) -> str:
					resp = unified.request("tools/call", {"name": tool_name, "arguments": args}, timeout_s=180.0)
					return _tool_text(resp)

				def _read_fixture(path: str) -> str:
					p = _safe_join(site_dir, path)
					return p.read_text(encoding="utf-8")

				def _write_fixture_file(path: str, content: str) -> str:
					path = (path or "").lstrip("/").strip()
					if path != "index.html":
						raise RuntimeError(f"Refusing write outside allowlist: {path!r}")
					p = _safe_join(site_dir, path)
					p.write_text(content, encoding="utf-8")
					return "ok"

				final_text = ""
				for _ in range(max_iters):
					chat = _openai_chat(
						api_key=openai_key,
						base_url=openai_base,
						model=model,
						messages=messages,
						tools=tools_for_llm,
						timeout_s=openai_timeout_s,
						max_retries=openai_retries,
					)
					msg = (((chat.get("choices") or [{}])[0]) or {}).get("message") or {}
					tool_calls = msg.get("tool_calls") or []

					if tool_calls:
						messages.append(msg)
						for call in tool_calls:
							if not isinstance(call, dict):
								continue
							call_id = call.get("id")
							fn = (call.get("function") or {}) if isinstance(call.get("function"), dict) else {}
							fn_name = fn.get("name")
							raw_args = fn.get("arguments") or "{}"
							try:
								args = json.loads(raw_args) if isinstance(raw_args, str) else {}
							except Exception:
								args = {}

							tool_calls_trace.append({"name": fn_name, "args": args})

							try:
								if fn_name == "mcp_tool_call":
									mcp_name = str(args.get("name") or "")
									out = _run_mcp_tool(mcp_name, args.get("arguments") or {})
									if mcp_name == "ui-describe.ui_describe":
										ui_describe_used_llm = "LLM not configured for ui-describe" not in out
								elif fn_name == "read_file":
									out = _read_fixture(str(args.get("path") or ""))
								elif fn_name == "write_file":
									out = _write_fixture_file(str(args.get("path") or ""), str(args.get("content") or ""))
								else:
									out = f"Error: unknown tool {fn_name!r}"
							except Exception as exc:  # noqa: BLE001
								out = f"Error: {type(exc).__name__}: {exc}"

							messages.append({"role": "tool", "tool_call_id": call_id, "content": out})
						continue

					final_text = (msg.get("content") or "").strip()
					if final_text:
						messages.append({"role": "assistant", "content": final_text})
					break

				# Post-validation: ensure key tools were used
				used = [t.get("name") for t in tool_calls_trace]
				if "write_file" not in used:
					raise RuntimeError("LLM did not call write_file; cannot validate fixes.")
				mcp_called = [t for t in tool_calls_trace if t.get("name") == "mcp_tool_call"]
				mcp_names = {str((t.get("args") or {}).get("name") or "") for t in mcp_called}
				required_mcp = {
					"context7_resolve_library_id",
					"context7_query_docs",
					"browser-use.browser_navigate",
					"chrome-devtools.list_console_messages",
					"chrome-devtools.evaluate_script",
					"ui-describe.ui_describe",
				}
				if not required_mcp <= mcp_names:
					raise RuntimeError(f"LLM did not call required MCP tools (missing: {sorted(required_mcp - mcp_names)})")
				if ui_describe_used_llm is False:
					raise RuntimeError("ui-describe did not use an LLM (missing/invalid OPENAI_* config?)")

				# Reload and verify deterministically
				reload_time = time.time()
				verify_url = f"{url}?v={int(reload_time)}"
				unified.request(
					"tools/call",
					{"name": "browser-use.browser_navigate", "arguments": {"url": verify_url}},
					timeout_s=60.0,
				)
				after_metrics = _poll_eval(
					unified,
					url_contains=url_contains,
					script=CONSOLE_METRICS_SCRIPT,
					timeout_s=4.0,
					predicate=lambda m: bool(m.get("okFlag") is True)
					and (str(m.get("statusText") or "").strip().upper() == "OK"),
				)

				console = unified.request(
					"tools/call",
					{"name": "chrome-devtools.list_console_messages", "arguments": {"url_contains": url_contains, "limit": 200}},
					timeout_s=45.0,
				)
				console_obj = _tool_json(console)
				msgs = console_obj.get("messages") if isinstance(console_obj, dict) else None
				new_err = []
				if isinstance(msgs, list):
					for m in msgs:
						if not isinstance(m, dict):
							continue
						tu = m.get("time_unix")
						if not isinstance(tu, (int, float)):
							continue
						if float(tu) < float(reload_time) - 0.05:
							continue
						typ = str(m.get("type") or "")
						if typ in {"exception", "error"}:
							new_err.append(m)

				ok = bool(after_metrics.get("okFlag") is True and (after_metrics.get("statusText") or "").strip().upper() == "OK")
				ok = ok and (len(new_err) == 0)

				return {
					"scenario": "console_fix",
					"ok": ok,
					"model": model,
					"ui_describe_used_llm": ui_describe_used_llm,
					"before_metrics": before_metrics,
					"after_metrics": after_metrics,
					"new_console_errors": new_err,
					"tool_calls": tool_calls_trace,
					"final_text": final_text,
				}
			finally:
				try:
					unified.close()
				finally:
					_cleanup_session_processes(state_dir=state_dir, session_id=session_id)


def _write_fixture_network_bug(site: Path) -> dict[str, Path]:
	site.mkdir(parents=True, exist_ok=True)
	index = site / "index.html"
	# Also serve a deterministic ping target.
	(site / "ping.txt").write_text("pong\n", encoding="utf-8")

	index.write_text(
		textwrap.dedent(
			"""\
			<!doctype html>
			<html lang="de">
			  <head>
			    <meta charset="utf-8" />
			    <meta name="viewport" content="width=device-width, initial-scale=1" />
			    <title>MCP Plus Live Network Lab</title>
			    <style>
			      body { margin: 0; font-family: system-ui, sans-serif; background: #0f1115; color: #e9ecf1; }
			      .wrap { max-width: 860px; margin: 0 auto; padding: 22px 18px; }
			      .card { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06); border-radius: 14px; padding: 18px; }
			      #value { font-weight: 700; }
			    </style>
			  </head>
			  <body>
			    <div class="wrap">
			      <div class="card">
			        <h1>Network Lab</h1>
			        <p>Diese Seite macht absichtlich einen kaputten Fetch (404). Der Live-Test soll ihn mit DevTools Network finden und fixen.</p>
			        <p>Wert: <span id="value">loading…</span></p>
			      </div>
			    </div>
			    <script>
			      async function loadValue() {
			        const el = document.getElementById('value');
			        try {
			          // Intentionally wrong path -> 404
			          const res = await fetch('/pong.txt');
			          const txt = (await res.text()).trim();
			          el.textContent = txt;
			        } catch (e) {
			          el.textContent = 'ERROR';
			          console.error('fetch failed', e);
			        }
			      }
			      loadValue();
			    </script>
			  </body>
			</html>
			"""
		),
		encoding="utf-8",
	)
	return {"index": index}


NETWORK_METRICS_SCRIPT = r"""
(() => {
  const el = document.querySelector('#value');
  return {
    valueText: el ? String(el.textContent || '') : null,
    title: document.title,
    href: location.href,
  };
})()
"""


def run_live_network_fix(
	*,
	model: str,
	max_iters: int,
	openai_timeout_s: float,
	openai_retries: int,
) -> dict[str, Any]:
	repo_root = Path(__file__).resolve().parents[1]
	python_bin = _require_env("BROWSER_USE_MCP_PYTHON")

	openai_key = _get_openai_api_key()
	openai_base = _resolve_openai_base_url()
	context7_key = _get_context7_api_key()

	session_id = f"live-llm-network-{int(time.time())}"

	with tempfile.TemporaryDirectory(prefix="browser-use-mcp-plus-live-network-") as tmp:
		tmp_path = Path(tmp)
		state_dir = tmp_path / "state"
		profile_dir = tmp_path / "profiles"
		site_dir = tmp_path / "site"
		state_dir.mkdir(parents=True, exist_ok=True)
		profile_dir.mkdir(parents=True, exist_ok=True)

		_write_fixture_network_bug(site_dir)

		common_env = {
			"BROWSER_USE_MCP_PYTHON": python_bin,
			"BROWSER_USE_SESSION_ID": session_id,
			"BROWSER_USE_CHROME_MODE": "session",
			"BROWSER_USE_ALLOW_HEADLESS_FALLBACK": "true",
			"BROWSER_USE_MCP_STATE_DIR": str(state_dir),
			"BROWSER_USE_CDP_PROFILE_BASE_DIR": str(profile_dir),
			"OPENAI_API_KEY": openai_key,
			"OPENAI_BASE_URL": openai_base,
			"CONTEXT7_API_KEY": context7_key,
			"UI_VISION_MODEL": (os.getenv("UI_VISION_MODEL") or model),
		}

		with serve_static_dir(site_dir) as (url, url_contains):
			unified = MCPStdioClient(
				name="mcp-plus",
				command=[str(repo_root / "bin" / "unified_mcp.sh")],
				env=common_env,
				cwd=str(repo_root),
			)
			unified.start()
			try:
				unified.initialize()

				tools_resp = unified.request("tools/list", {}, timeout_s=45.0)
				tools = (tools_resp.get("result") or {}).get("tools") or []
				names = {t.get("name") for t in tools if isinstance(t, dict)}
				required_tools = {
					"browser-use.browser_navigate",
					"ui-describe.ui_describe",
					"chrome-devtools.evaluate_script",
					"chrome-devtools.list_network_requests",
					"context7_resolve_library_id",
					"context7_query_docs",
				}
				missing = sorted(required_tools - names)
				if missing:
					raise RuntimeError(f"Unified server missing tools: {missing}")

				baseline_url = f"{url}?v={int(time.time())}"
				unified.request(
					"tools/call",
					{"name": "browser-use.browser_navigate", "arguments": {"url": baseline_url}},
					timeout_s=60.0,
				)
				before_metrics = _poll_eval(
					unified,
					url_contains=url_contains,
					script=NETWORK_METRICS_SCRIPT,
					timeout_s=2.5,
					predicate=lambda m: str(m.get("valueText") or "").strip().lower() != "loading…",
				)

				tool_calls_trace: list[dict[str, Any]] = []
				ui_describe_used_llm: bool | None = None

				tools_for_llm = [
					{
						"type": "function",
						"function": {
							"name": "mcp_tool_call",
							"description": "Call a tool exposed by the unified MCP server (by exact tool name).",
							"parameters": {
								"type": "object",
								"properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
								"required": ["name", "arguments"],
							},
						},
					},
					{
						"type": "function",
						"function": {
							"name": "read_file",
							"description": "Read a fixture file under the provided fixture root.",
							"parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
						},
					},
					{
						"type": "function",
						"function": {
							"name": "write_file",
							"description": "Write a fixture file under the provided fixture root.",
							"parameters": {
								"type": "object",
								"properties": {"path": {"type": "string"}, "content": {"type": "string"}},
								"required": ["path", "content"],
							},
						},
					},
				]

				system = (
					"Du bist ein QA+Fix Agent. Ziel: einen kaputten Fetch (404) finden und beheben.\n\n"
					"Nutze mindestens einmal:\n"
					"- context7_resolve_library_id + context7_query_docs (kurz, z.B. Playwright Network/CDP)\n"
					"- browser-use.browser_navigate\n"
					"- chrome-devtools.list_network_requests (soll 404 zeigen)\n"
					"- ui-describe.ui_describe (Screenshot prüfen)\n"
					"- chrome-devtools.evaluate_script\n"
					"- write_file (index.html fixen)\n\n"
					"Erwartung nach Fix:\n"
					"- Fetch geht auf /ping.txt\n"
					"- #value zeigt 'pong'\n"
					"Du darfst nur in index.html schreiben."
				)
				user = (
					"URL: {url}\n"
					"url_contains: {url_contains}\n\n"
					"Vorgehen:\n"
					"1) Prüfe Network Requests via chrome-devtools.list_network_requests und finde den 404.\n"
					"2) Fixe index.html (fetch Pfad).\n"
					"3) Verifiziere per chrome-devtools.evaluate_script (NETWORK_METRICS_SCRIPT) + list_network_requests.\n"
				).format(url=baseline_url, url_contains=url_contains)

				messages: list[dict[str, Any]] = [
					{"role": "system", "content": system},
					{"role": "user", "content": user},
				]

				def _run_mcp_tool(tool_name: str, args: dict[str, Any]) -> str:
					resp = unified.request("tools/call", {"name": tool_name, "arguments": args}, timeout_s=180.0)
					return _tool_text(resp)

				def _read_fixture(path: str) -> str:
					p = _safe_join(site_dir, path)
					return p.read_text(encoding="utf-8")

				def _write_fixture_file(path: str, content: str) -> str:
					path = (path or "").lstrip("/").strip()
					if path != "index.html":
						raise RuntimeError(f"Refusing write outside allowlist: {path!r}")
					p = _safe_join(site_dir, path)
					p.write_text(content, encoding="utf-8")
					return "ok"

				final_text = ""
				for _ in range(max_iters):
					chat = _openai_chat(
						api_key=openai_key,
						base_url=openai_base,
						model=model,
						messages=messages,
						tools=tools_for_llm,
						timeout_s=openai_timeout_s,
						max_retries=openai_retries,
					)
					msg = (((chat.get("choices") or [{}])[0]) or {}).get("message") or {}
					tool_calls = msg.get("tool_calls") or []

					if tool_calls:
						messages.append(msg)
						for call in tool_calls:
							if not isinstance(call, dict):
								continue
							call_id = call.get("id")
							fn = (call.get("function") or {}) if isinstance(call.get("function"), dict) else {}
							fn_name = fn.get("name")
							raw_args = fn.get("arguments") or "{}"
							try:
								args = json.loads(raw_args) if isinstance(raw_args, str) else {}
							except Exception:
								args = {}

							tool_calls_trace.append({"name": fn_name, "args": args})

							try:
								if fn_name == "mcp_tool_call":
									mcp_name = str(args.get("name") or "")
									out = _run_mcp_tool(mcp_name, args.get("arguments") or {})
									if mcp_name == "ui-describe.ui_describe":
										ui_describe_used_llm = "LLM not configured for ui-describe" not in out
								elif fn_name == "read_file":
									out = _read_fixture(str(args.get("path") or ""))
								elif fn_name == "write_file":
									out = _write_fixture_file(str(args.get("path") or ""), str(args.get("content") or ""))
								else:
									out = f"Error: unknown tool {fn_name!r}"
							except Exception as exc:  # noqa: BLE001
								out = f"Error: {type(exc).__name__}: {exc}"

							messages.append({"role": "tool", "tool_call_id": call_id, "content": out})
						continue

					final_text = (msg.get("content") or "").strip()
					if final_text:
						messages.append({"role": "assistant", "content": final_text})
					break

				used = [t.get("name") for t in tool_calls_trace]
				if "write_file" not in used:
					raise RuntimeError("LLM did not call write_file; cannot validate fixes.")
				mcp_called = [t for t in tool_calls_trace if t.get("name") == "mcp_tool_call"]
				mcp_names = {str((t.get("args") or {}).get("name") or "") for t in mcp_called}
				required_mcp = {
					"context7_resolve_library_id",
					"context7_query_docs",
					"browser-use.browser_navigate",
					"chrome-devtools.list_network_requests",
					"chrome-devtools.evaluate_script",
					"ui-describe.ui_describe",
				}
				if not required_mcp <= mcp_names:
					raise RuntimeError(f"LLM did not call required MCP tools (missing: {sorted(required_mcp - mcp_names)})")
				if ui_describe_used_llm is False:
					raise RuntimeError("ui-describe did not use an LLM (missing/invalid OPENAI_* config?)")

				verify_time = time.time()
				verify_url = f"{url}?v={int(verify_time)}"
				unified.request(
					"tools/call",
					{"name": "browser-use.browser_navigate", "arguments": {"url": verify_url}},
					timeout_s=60.0,
				)
				after_metrics = _poll_eval(
					unified,
					url_contains=url_contains,
					script=NETWORK_METRICS_SCRIPT,
					timeout_s=4.0,
					predicate=lambda m: str(m.get("valueText") or "").strip().lower() == "pong",
				)
				ping_ok, _net_obj = _poll_network_ping_ok(unified, url_contains=url_contains, timeout_s=4.0)

				value = str(after_metrics.get("valueText") or "").strip().lower()
				ok = (value == "pong") and ping_ok

				return {
					"scenario": "network_fix",
					"ok": ok,
					"model": model,
					"ui_describe_used_llm": ui_describe_used_llm,
					"before_metrics": before_metrics,
					"after_metrics": after_metrics,
					"ping_ok": ping_ok,
					"tool_calls": tool_calls_trace,
					"final_text": final_text,
				}
			finally:
				try:
					unified.close()
				finally:
					_cleanup_session_processes(state_dir=state_dir, session_id=session_id)


def main(argv: list[str]) -> int:
	parser = argparse.ArgumentParser(description="Live LLM suite: multiple MCP scenarios + model discovery.")
	parser.add_argument("--model", type=str, default=os.getenv("MCP_PLUS_LIVE_MODEL", "gpt-4o-mini"))
	parser.add_argument(
		"--vision-model",
		type=str,
		default=os.getenv("UI_VISION_MODEL", ""),
		help="Override UI_VISION_MODEL for ui-describe (defaults to --model).",
	)
	parser.add_argument("--max-iters", type=int, default=int(os.getenv("MCP_PLUS_LIVE_MAX_ITERS", "14")))
	parser.add_argument("--runs", type=int, default=int(os.getenv("MCP_PLUS_LIVE_RUNS", "1")))
	parser.add_argument("--openai-timeout-s", type=float, default=float(os.getenv("MCP_PLUS_LIVE_OPENAI_TIMEOUT_S", "60")))
	parser.add_argument("--openai-retries", type=int, default=int(os.getenv("MCP_PLUS_LIVE_OPENAI_RETRIES", "2")))
	parser.add_argument("--list-models", action="store_true", help="List models from OPENAI_BASE_URL and exit.")
	parser.add_argument("--require-model", action="store_true", help="Fail if --model is not present in /models.")
	args = parser.parse_args(argv)

	api_key = _get_openai_api_key()
	base_url = _resolve_openai_base_url()

	models: list[str] | None = None
	model_list_error: str | None = None
	try:
		models = _openai_list_models(api_key=api_key, base_url=base_url)
	except Exception as exc:  # noqa: BLE001
		model_list_error = f"{type(exc).__name__}: {exc}"

	if args.list_models:
		print(
			json.dumps(
				{
					"ok": models is not None,
					"base_url": base_url,
					"models": models,
					"error": model_list_error,
				},
				ensure_ascii=False,
				indent=2,
			)
		)
		return 0 if models is not None else 2

	# Only required when actually running scenarios (not for model listing).
	_require_env("BROWSER_USE_MCP_PYTHON")

	if args.require_model and models is None:
		print(json.dumps({"ok": False, "error": f"Failed to fetch /models: {model_list_error}"}, ensure_ascii=False, indent=2))
		return 2

	model = _pick_model(requested=str(args.model), models=models or [])
	if str(args.vision_model or "").strip():
		os.environ["UI_VISION_MODEL"] = str(args.vision_model).strip()

	if args.require_model and models is not None and model not in models:
		print(
			json.dumps(
				{"ok": False, "error": f"Requested model not found in /models: {model}", "models": models},
				ensure_ascii=False,
				indent=2,
			)
		)
		return 2

	# Run 3 scenarios per run:
	# - UI overlap+contrast fix (reuses the single-scenario runner)
	# - Console error fix
	# - Network 404 fix
	from scripts.live_llm_e2e import run_live_e2e  # noqa: PLC0415

	all_results: list[dict[str, Any]] = []
	ok = True

	for run_idx in range(max(1, int(args.runs))):
		try:
			# Propagate run-index into session id indirectly via env; the per-scenario runner uses time-based ids.
			os.environ["MCP_PLUS_LIVE_RUN_INDEX"] = str(run_idx)
		except Exception:
			pass

		try:
			ui = run_live_e2e(
				model=str(model),
				max_iters=int(args.max_iters),
				openai_timeout_s=float(args.openai_timeout_s),
				openai_retries=int(args.openai_retries),
			)
			all_results.append(
				{
					"scenario": "ui_fix",
					"ok": ui.ok,
					"model": ui.model,
					"ui_describe_used_llm": ui.ui_describe_used_llm,
					"before_metrics": ui.before_metrics,
					"after_metrics": ui.after_metrics,
					"tool_calls": ui.tool_calls,
					"final_text": ui.final_text,
				}
			)
			ok = ok and bool(ui.ok)
		except Exception as exc:  # noqa: BLE001
			ok = False
			all_results.append({"scenario": "ui_fix", "ok": False, "error": f"{type(exc).__name__}: {exc}"})

		for fn in (run_live_console_fix, run_live_network_fix):
			try:
				out = fn(
					model=str(model),
					max_iters=int(args.max_iters),
					openai_timeout_s=float(args.openai_timeout_s),
					openai_retries=int(args.openai_retries),
				)
				all_results.append(out)
				ok = ok and bool(out.get("ok") is True)
			except Exception as exc:  # noqa: BLE001
				ok = False
				all_results.append({"scenario": getattr(fn, "__name__", "scenario"), "ok": False, "error": f"{type(exc).__name__}: {exc}"})

	print(
		json.dumps(
			{
				"ok": ok,
				"base_url": base_url,
				"model": str(model),
				"vision_model": os.getenv("UI_VISION_MODEL") or str(model),
				"models": models,
				"models_error": model_list_error,
				"results": all_results,
			},
			ensure_ascii=False,
			indent=2,
		)
	)
	return 0 if ok else 1


if __name__ == "__main__":
	raise SystemExit(main(list(__import__("sys").argv[1:])))
