from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import re
import signal
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from mcp_plus.fixture_server import serve_static_dir
from mcp_plus.stdio_client import MCPStdioClient


def _tool_text(resp: dict) -> str:
	return (((resp.get("result") or {}).get("content") or [{}])[0] or {}).get("text") or ""


def _require_env(name: str) -> str:
	val = (os.getenv(name) or "").strip()
	if not val:
		raise RuntimeError(f"Missing env var: {name}")
	return val


def _load_codex_auth_openai_key() -> str | None:
	path = Path.home() / ".codex" / "auth.json"
	try:
		obj = json.loads(path.read_text(encoding="utf-8"))
	except Exception:
		return None
	val = obj.get("OPENAI_API_KEY") if isinstance(obj, dict) else None
	if isinstance(val, str) and val.strip():
		return val.strip()
	return None


def _load_context7_key_from_codex_config() -> str | None:
	path = Path.home() / ".codex" / "config.toml"
	try:
		import tomllib  # Python 3.11+

		obj = tomllib.loads(path.read_text(encoding="utf-8"))
	except Exception:
		return None

	mcp_servers = obj.get("mcp_servers") if isinstance(obj, dict) else None
	context7 = (mcp_servers or {}).get("context7") if isinstance(mcp_servers, dict) else None
	args = context7.get("args") if isinstance(context7, dict) else None
	if not isinstance(args, list):
		return None

	# Expect: ["-y", "@upstash/context7-mcp", "--api-key", "<KEY>"]
	for idx, val in enumerate(args):
		if val == "--api-key" and idx + 1 < len(args):
			key = args[idx + 1]
			if isinstance(key, str) and key.strip():
				return key.strip()
		if isinstance(val, str) and val.startswith("--api-key="):
			key = val.split("=", 1)[1].strip()
			if key:
				return key
	return None


def _get_openai_api_key() -> str:
	val = (os.getenv("OPENAI_API_KEY") or "").strip()
	if val:
		return val
	fallback = _load_codex_auth_openai_key()
	if fallback:
		return fallback
	raise RuntimeError("Missing OPENAI_API_KEY (set env var or login via Codex so ~/.codex/auth.json exists).")


def _get_context7_api_key() -> str:
	val = (os.getenv("CONTEXT7_API_KEY") or "").strip()
	if val:
		return val
	fallback = _load_context7_key_from_codex_config()
	if fallback:
		return fallback
	raise RuntimeError("Missing CONTEXT7_API_KEY (set env var or configure ~/.codex/config.toml mcp_servers.context7).")


def _resolve_openai_base_url() -> str:
	base = (os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or "").strip()
	if base:
		return base.rstrip("/")
	return "https://api.openai.com/v1"


def _sanitize_session_id(raw: str) -> str:
	keep = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@+-"
	safe = "".join((ch if ch in keep else "_") for ch in (raw or ""))
	safe = safe.strip("_") or "session"
	return safe[:80]


def _read_pid_file(path: Path) -> int | None:
	try:
		raw = path.read_text(encoding="utf-8", errors="ignore")
	except Exception:
		return None
	digits = "".join(ch for ch in raw if ch.isdigit())
	if not digits:
		return None
	try:
		pid = int(digits)
	except Exception:
		return None
	return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
	try:
		os.kill(pid, 0)
		return True
	except Exception:
		return False


def _kill_pid(pid: int) -> None:
	try:
		os.kill(pid, signal.SIGTERM)
	except Exception:
		return
	for _ in range(40):
		if not _pid_alive(pid):
			return
		time.sleep(0.05)
	try:
		os.kill(pid, signal.SIGKILL)
	except Exception:
		return


def _cleanup_session_processes(*, state_dir: Path, session_id: str) -> None:
	session_dir = state_dir / "sessions" / _sanitize_session_id(session_id)
	reaper_pid = _read_pid_file(session_dir / "chrome.reaper.pid")
	chrome_pid = _read_pid_file(session_dir / "chrome.pid")

	if reaper_pid:
		_kill_pid(reaper_pid)
	if chrome_pid:
		_kill_pid(chrome_pid)


def _safe_join(root: Path, rel: str) -> Path:
	rel = (rel or "").lstrip("/").strip()
	p = (root / rel).resolve()
	root_resolved = root.resolve()
	if p == root_resolved or root_resolved not in p.parents:
		raise RuntimeError(f"Refusing path outside fixture root: {rel!r}")
	return p


def _write_fixture(site: Path) -> dict[str, Path]:
	site.mkdir(parents=True, exist_ok=True)
	index = site / "index.html"
	styles = site / "styles.css"

	index.write_text(
		textwrap.dedent(
			"""\
			<!doctype html>
			<html lang="de">
			  <head>
			    <meta charset="utf-8" />
			    <meta name="viewport" content="width=device-width, initial-scale=1" />
			    <title>MCP Plus Live UI Lab</title>
			    <link rel="stylesheet" href="styles.css" />
			  </head>
			  <body>
			    <header class="topbar">
			      <div class="topbar__inner">
			        <h1 class="brand">Live UI Lab</h1>
			        <button id="primary">Weiter</button>
			      </div>
			    </header>
			    <main class="content">
			      <section class="card">
			        <h2>Willkommen</h2>
			        <p class="lead">
			          Diese Seite hat absichtlich UI-Probleme (Overlap + schlechter Kontrast). Der Live-Test soll das per
			          Screenshot erkennen und beheben.
			        </p>
			        <div class="field">
			          <label for="email">E-Mail</label>
			          <input id="email" placeholder="name@example.com" />
			        </div>
			        <p class="hint">Tipp: Button sollte gut lesbar sein und nichts sollte vom Header überdeckt werden.</p>
			      </section>
			    </main>
			  </body>
			</html>
			"""
		),
		encoding="utf-8",
	)

	# Intentionally flawed CSS:
	# - Fixed header overlays main content (no padding-top on main)
	# - Button has very low contrast (fg almost same as bg)
	styles.write_text(
		textwrap.dedent(
			"""\
			:root {
			  --bg: #0f1115;
			  --panel: #121520;
			  --text: #101216; /* intentionally too dark */
			  --muted: #30344a;
			}

			* { box-sizing: border-box; }
			body {
			  margin: 0;
			  font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
			  background: var(--bg);
			  color: var(--text);
			}

			.topbar {
			  position: fixed;
			  inset: 0 0 auto 0;
			  height: 84px;
			  background: var(--panel);
			  border-bottom: 1px solid rgba(255,255,255,0.06);
			  z-index: 10;
			}
			.topbar__inner {
			  height: 100%;
			  display: flex;
			  align-items: center;
			  justify-content: space-between;
			  padding: 0 18px;
			  gap: 12px;
			}
			.brand {
			  margin: 0;
			  font-size: 18px;
			  letter-spacing: 0.2px;
			}

			/* main content is intentionally missing top padding -> overlap */
			.content {
			  padding: 24px 18px;
			}

			.card {
			  max-width: 720px;
			  margin: 0 auto;
			  background: rgba(255,255,255,0.03);
			  border: 1px solid rgba(255,255,255,0.06);
			  border-radius: 14px;
			  padding: 18px;
			  backdrop-filter: blur(8px);
			}
			.lead {
			  margin-top: 8px;
			  line-height: 1.4;
			  color: rgba(255,255,255,0.62);
			}

			.field {
			  margin-top: 18px;
			  display: grid;
			  gap: 10px;
			}
			label {
			  font-size: 14px;
			  color: rgba(255,255,255,0.6);
			}
			input {
			  width: 100%;
			  padding: 12px 14px;
			  border-radius: 10px;
			  border: 1px solid rgba(255,255,255,0.08);
			  background: rgba(0,0,0,0.2);
			  color: rgba(255,255,255,0.78);
			  outline: none;
			}

			#primary {
			  border-radius: 10px;
			  padding: 10px 14px;
			  border: 1px solid rgba(255,255,255,0.06);
			  background: #1b1f2a;
			  color: #1c202b; /* intentionally low contrast */
			}

			.hint {
			  margin-top: 14px;
			  font-size: 13px;
			  color: rgba(255,255,255,0.55);
			}
			"""
		),
		encoding="utf-8",
	)

	return {"index": index, "styles": styles}


UI_METRICS_SCRIPT = r"""
(() => {
  function parseRgb(v) {
    const m = String(v || "").match(/rgba?\(([^)]+)\)/i);
    if (!m) return [0,0,0,1];
    const parts = m[1].split(",").map(s => s.trim());
    const r = Number(parts[0] || 0);
    const g = Number(parts[1] || 0);
    const b = Number(parts[2] || 0);
    const a = parts.length > 3 ? Number(parts[3]) : 1;
    return [r,g,b,Number.isFinite(a) ? a : 1];
  }

  function srgbToLin(c) {
    const s = c / 255;
    return s <= 0.04045 ? (s / 12.92) : Math.pow((s + 0.055) / 1.055, 2.4);
  }

  function luminance(rgb) {
    const r = srgbToLin(rgb[0]);
    const g = srgbToLin(rgb[1]);
    const b = srgbToLin(rgb[2]);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }

  function contrastRatio(fg, bg) {
    const L1 = luminance(fg);
    const L2 = luminance(bg);
    const lighter = Math.max(L1, L2);
    const darker = Math.min(L1, L2);
    return (lighter + 0.05) / (darker + 0.05);
  }

  function rect(el) {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height, top: r.top, left: r.left, right: r.right, bottom: r.bottom };
  }

  function overlaps(a, b) {
    if (!a || !b) return false;
    return !(a.right <= b.left || a.left >= b.right || a.bottom <= b.top || a.top >= b.bottom);
  }

  const header = document.querySelector(".topbar");
  const card = document.querySelector(".card");
  const button = document.querySelector("#primary");
  const headerRect = rect(header);
  const cardRect = rect(card);
  const overlap = overlaps(headerRect, cardRect);

  let contrast = null;
  try {
    const s = getComputedStyle(button);
    const fg = parseRgb(s.color);
    const bg = parseRgb(s.backgroundColor);
    contrast = contrastRatio(fg, bg);
  } catch (e) {
    contrast = null;
  }

  return {
    overlap,
    contrast,
    headerRect,
    cardRect,
    href: location.href,
    title: document.title,
  };
})()
"""


def _openai_chat(
	*,
	api_key: str,
	base_url: str,
	model: str,
	messages: list[dict[str, Any]],
	tools: list[dict[str, Any]],
	timeout_s: float,
	max_retries: int,
) -> dict[str, Any]:
	url = f"{base_url.rstrip('/')}/chat/completions"
	payload = {
		"model": model,
		"messages": messages,
		"tools": tools,
		"tool_choice": "auto",
		"temperature": 0.2,
	}
	data = json.dumps(payload).encode("utf-8")
	headers = {
		"Authorization": f"Bearer {api_key}",
		"Content-Type": "application/json",
	}

	last_err: Exception | None = None
	for attempt in range(max_retries + 1):
		try:
			req = urllib.request.Request(url, data=data, headers=headers, method="POST")
			with urllib.request.urlopen(req, timeout=timeout_s) as resp:
				raw = resp.read().decode("utf-8", errors="replace")
			return json.loads(raw)
		except urllib.error.HTTPError as exc:
			raw = exc.read().decode("utf-8", errors="replace") if getattr(exc, "fp", None) else str(exc)
			last_err = RuntimeError(f"OpenAI HTTPError {exc.code}: {raw[:2000]}")
		except Exception as exc:  # noqa: BLE001
			last_err = exc
		if attempt < max_retries:
			time.sleep(0.75 * (2**attempt) + random.random() * 0.2)
			continue
		raise RuntimeError("OpenAI request failed") from last_err


@dataclass
class LiveRunResult:
	ok: bool
	model: str
	tool_calls: list[dict[str, Any]]
	final_text: str
	before_metrics: dict[str, Any]
	after_metrics: dict[str, Any]


def _extract_eval_result(text: str) -> dict[str, Any]:
	try:
		obj = json.loads(text)
	except Exception as exc:  # noqa: BLE001
		raise RuntimeError(f"Expected JSON eval result, got: {text[:400]!r}") from exc
	res = obj.get("result")
	if not isinstance(res, dict):
		raise RuntimeError(f"Unexpected eval 'result' type: {type(res)}")
	return res


def run_live_e2e(*, model: str, max_iters: int, openai_timeout_s: float, openai_retries: int) -> LiveRunResult:
	repo_root = Path(__file__).resolve().parents[1]
	python_bin = _require_env("BROWSER_USE_MCP_PYTHON")

	openai_key = _get_openai_api_key()
	openai_base = _resolve_openai_base_url()
	context7_key = _get_context7_api_key()

	session_id = f"live-llm-e2e-{int(time.time())}"

	with tempfile.TemporaryDirectory(prefix="browser-use-mcp-plus-live-e2e-") as tmp:
		tmp_path = Path(tmp)
		state_dir = tmp_path / "state"
		profile_dir = tmp_path / "profiles"
		site_dir = tmp_path / "site"
		state_dir.mkdir(parents=True, exist_ok=True)
		profile_dir.mkdir(parents=True, exist_ok=True)

		files = _write_fixture(site_dir)

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
			# Make UI describe default usable for OpenAI as well.
			"UI_VISION_MODEL": os.getenv("UI_VISION_MODEL", "gpt-4o-mini"),
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

				# Ensure critical tools exist
				tools_resp = unified.request("tools/list", {}, timeout_s=45.0)
				tools = (tools_resp.get("result") or {}).get("tools") or []
				names = {t.get("name") for t in tools if isinstance(t, dict)}
				required = {
					"browser-use.browser_navigate",
					"ui-describe.ui_describe",
					"chrome-devtools.evaluate_script",
					"context7_resolve_library_id",
					"context7_query_docs",
				}
				missing = sorted(required - names)
				if missing:
					raise RuntimeError(f"Unified server missing tools: {missing}")

				# Baseline: navigate + metrics
				unified.request(
					"tools/call",
					{"name": "browser-use.browser_navigate", "arguments": {"url": url}},
					timeout_s=60.0,
				)
				before_eval = unified.request(
					"tools/call",
					{"name": "chrome-devtools.evaluate_script", "arguments": {"url_contains": url_contains, "script": UI_METRICS_SCRIPT}},
					timeout_s=45.0,
				)
				before_metrics = _extract_eval_result(_tool_text(before_eval))

				tool_calls_trace: list[dict[str, Any]] = []

				tools_for_llm = [
					{
						"type": "function",
						"function": {
							"name": "mcp_tool_call",
							"description": "Call a tool exposed by the unified MCP server (by exact tool name).",
							"parameters": {
								"type": "object",
								"properties": {
									"name": {"type": "string"},
									"arguments": {"type": "object"},
								},
								"required": ["name", "arguments"],
							},
						},
					},
					{
						"type": "function",
						"function": {
							"name": "read_file",
							"description": "Read a fixture file under the provided fixture root.",
							"parameters": {
								"type": "object",
								"properties": {"path": {"type": "string"}},
								"required": ["path"],
							},
						},
					},
					{
						"type": "function",
						"function": {
							"name": "write_file",
							"description": "Write a fixture file under the provided fixture root.",
							"parameters": {
								"type": "object",
								"properties": {
									"path": {"type": "string"},
									"content": {"type": "string"},
								},
								"required": ["path", "content"],
							},
						},
					},
				]

				system = (
					"Du bist ein QA+UI Agent. Benutze die Tools, um (1) über den Stack zu recherchieren (Context7), "
					"(2) die Web-UI zu testen (browser-use + ui-describe) und (3) die UI zu verbessern, indem du die "
					"Fixture-Dateien änderst. Danach verifiziere per chrome-devtools/evaluate_script, dass:\n"
					"- overlap == false (Header überdeckt keine Card)\n"
					"- contrast >= 4.5 (Button #primary Text vs Background)\n\n"
					"Nutze mindestens einmal:\n"
					"- context7_resolve_library_id + context7_query_docs\n"
					"- ui-describe.ui_describe\n"
					"- write_file (um styles.css zu fixen)\n"
					"Du darfst nur in index.html/styles.css schreiben."
				)
				user = (
					"Ziel-URL: {url}\n"
					"url_contains: {url_contains}\n"
					"Fixture root (nur zur Orientierung, nicht als Datei-Pfad verwenden): {root}\n\n"
					"Relevante MCP Tools (via mcp_tool_call.name):\n"
					"- context7_resolve_library_id\n"
					"- context7_query_docs\n"
					"- browser-use.browser_navigate\n"
					"- ui-describe.ui_describe\n"
					"- chrome-devtools.evaluate_script\n\n"
					"Starte mit einer kurzen Stack-Recherche (z.B. Playwright connect_over_cdp) via Context7, "
					"dann prüfe die UI per Screenshot-Beschreibung (ui-describe) und verbessere die UI "
					"(insbesondere Kontrast + Overlap)."
				).format(url=url, url_contains=url_contains, root=str(site_dir))

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
					if path not in {"index.html", "styles.css"}:
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
									out = _run_mcp_tool(str(args.get("name") or ""), args.get("arguments") or {})
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

				# Post-validation: ensure the key tools were actually used
				used = [t.get("name") for t in tool_calls_trace]
				if "write_file" not in used:
					raise RuntimeError("LLM did not call write_file; cannot validate UI improvements.")
				if "mcp_tool_call" not in used:
					raise RuntimeError("LLM did not call any MCP tools.")
				# Ensure it likely used Context7 + ui-describe (best-effort check on args)
				mcp_called = [t for t in tool_calls_trace if t.get("name") == "mcp_tool_call"]
				mcp_names = {str((t.get("args") or {}).get("name") or "") for t in mcp_called}
				if not {"context7_resolve_library_id", "context7_query_docs"} <= mcp_names:
					raise RuntimeError(f"LLM did not use Context7 tools (saw: {sorted(mcp_names)})")
				if "ui-describe.ui_describe" not in mcp_names:
					raise RuntimeError("LLM did not call ui-describe.ui_describe")

				# Recompute metrics
				unified.request(
					"tools/call",
					{"name": "browser-use.browser_navigate", "arguments": {"url": url}},
					timeout_s=60.0,
				)
				after_eval = unified.request(
					"tools/call",
					{"name": "chrome-devtools.evaluate_script", "arguments": {"url_contains": url_contains, "script": UI_METRICS_SCRIPT}},
					timeout_s=45.0,
				)
				after_metrics = _extract_eval_result(_tool_text(after_eval))

				ok = bool(after_metrics.get("overlap") is False)
				contrast = after_metrics.get("contrast")
				if isinstance(contrast, (int, float)):
					ok = ok and float(contrast) >= 4.5
				else:
					ok = False

				return LiveRunResult(
					ok=ok,
					model=model,
					tool_calls=tool_calls_trace,
					final_text=final_text,
					before_metrics=before_metrics,
					after_metrics=after_metrics,
				)
			finally:
				try:
					unified.close()
				finally:
					_cleanup_session_processes(state_dir=state_dir, session_id=session_id)


def main(argv: list[str]) -> int:
	parser = argparse.ArgumentParser(description="Live LLM E2E: Context7 research + UI test + auto-fix + verify.")
	parser.add_argument("--model", type=str, default=os.getenv("MCP_PLUS_LIVE_MODEL", "gpt-4o-mini"))
	parser.add_argument("--max-iters", type=int, default=int(os.getenv("MCP_PLUS_LIVE_MAX_ITERS", "12")))
	parser.add_argument("--openai-timeout-s", type=float, default=float(os.getenv("MCP_PLUS_LIVE_OPENAI_TIMEOUT_S", "60")))
	parser.add_argument("--openai-retries", type=int, default=int(os.getenv("MCP_PLUS_LIVE_OPENAI_RETRIES", "2")))
	args = parser.parse_args(argv)

	try:
		result = run_live_e2e(
			model=str(args.model),
			max_iters=int(args.max_iters),
			openai_timeout_s=float(args.openai_timeout_s),
			openai_retries=int(args.openai_retries),
		)
	except Exception as exc:
		print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
		return 2

	print(
		json.dumps(
			{
				"ok": result.ok,
				"model": result.model,
				"before_metrics": result.before_metrics,
				"after_metrics": result.after_metrics,
				"tool_calls": result.tool_calls,
				"final_text": result.final_text,
			},
			ensure_ascii=False,
			indent=2,
		)
	)
	return 0 if result.ok else 1


if __name__ == "__main__":
	raise SystemExit(main(list(__import__("sys").argv[1:])))
