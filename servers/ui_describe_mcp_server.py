import asyncio
import base64
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

os.environ.setdefault('NODE_NO_WARNINGS', '1')

from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, SystemMessage, UserMessage
from browser_use.llm.openai.chat import ChatOpenAI

try:
	import mcp.server.stdio
	import mcp.types as types
	from mcp.server import NotificationOptions, Server
	from mcp.server.models import InitializationOptions

	MCP_AVAILABLE = True
except Exception:
	MCP_AVAILABLE = False


logging.basicConfig(
	stream=sys.stderr,
	level=logging.WARNING,
	format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
	force=True,
)
logger = logging.getLogger('ui-describe-mcp')

# Prevent MCP SDK debug/info logs from polluting stdout (stdio transport).
logging.getLogger('mcp').setLevel(logging.ERROR)
logging.getLogger('mcp').propagate = False


def _env_bool(name: str, default: bool = False) -> bool:
	val = (os.getenv(name) or '').strip().lower()
	if not val:
		return default
	return val in {'1', 'true', 'yes', 'y', 'on'}


def _get_cdp_url() -> str:
	# Prefer chrome.json in the active session folder (keeps working after CDP restarts),
	# otherwise fall back to env vars.
	state_path = _get_shared_state_path()
	try:
		chrome_state = state_path.parent / 'chrome.json'
		if chrome_state.exists():
			obj = json.loads(chrome_state.read_text(encoding='utf-8'))
			if isinstance(obj, dict):
				cdp_url = (obj.get('cdp_url') or '').strip()
				if cdp_url:
					return cdp_url
	except Exception:
		pass
	return (os.getenv('UI_CDP_URL') or os.getenv('BROWSER_USE_CDP_URL') or 'http://127.0.0.1:9222').strip()


def _looks_like_cdp_connect_error(exc: Exception) -> bool:
	msg = str(exc)
	return any(token in msg for token in ('connect ECONNREFUSED', 'ECONNREFUSED', 'connect_over_cdp', 'Failed to connect'))

def _repo_root() -> Path:
	return Path(__file__).resolve().parents[1]

def _default_state_root() -> Path:
	explicit = (os.getenv('BROWSER_USE_MCP_STATE_DIR') or '').strip()
	if explicit:
		return Path(explicit).expanduser()
	xdg = (os.getenv('XDG_STATE_HOME') or '').strip()
	if xdg:
		return Path(xdg).expanduser() / 'browser-use-mcp-plus'
	return Path('~/.local/state/browser-use-mcp-plus').expanduser()


def _ensure_cdp_chrome_ready() -> None:
	explicit = (os.getenv('BROWSER_USE_MCP_ENSURE_CHROME_SCRIPT') or '').strip()
	script = Path(explicit).expanduser() if explicit else (_repo_root() / 'bin' / 'ensure_cdp_chrome.sh')
	if not script.exists():
		return
	subprocess.run(
		['bash', str(script)],
		check=True,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		timeout=45,
	)

def _pid_alive(pid: int) -> bool:
	try:
		os.kill(pid, 0)
		return True
	except Exception:
		return False


def _read_pid(path: Path) -> int | None:
	try:
		raw = path.read_text(encoding='utf-8', errors='ignore')
	except Exception:
		return None
	digits = ''.join(ch for ch in raw if ch.isdigit())
	if not digits:
		return None
	try:
		pid = int(digits)
	except Exception:
		return None
	return pid if pid > 0 else None


def _session_dir_from_state() -> Path:
	return _get_shared_state_path().parent


def set_browser_keep_open(keep_open: bool) -> dict[str, Any]:
	session_dir = _session_dir_from_state()
	marker = session_dir / 'keep-browser-open.flag'
	reaper_pid_path = session_dir / 'chrome.reaper.pid'

	out: dict[str, Any] = {
		'session_dir': str(session_dir),
		'marker_file': str(marker),
		'reaper_pid_file': str(reaper_pid_path),
		'keep_open_requested': bool(keep_open),
		'marker_present_before': marker.exists(),
		'reaper_pid_before': None,
		'reaper_killed': False,
		'note': None,
	}

	if keep_open:
		session_dir.mkdir(parents=True, exist_ok=True)
		marker.write_text(f'enabled_at_unix={time.time()}\n', encoding='utf-8')

		pid = _read_pid(reaper_pid_path) if reaper_pid_path.exists() else None
		out['reaper_pid_before'] = pid

		if pid and _pid_alive(pid):
			try:
				os.kill(pid, signal.SIGTERM)
				out['reaper_killed'] = True
			except Exception:
				out['reaper_killed'] = False

			for _ in range(10):
				if not _pid_alive(pid):
					break
				time.sleep(0.05)
			if _pid_alive(pid):
				try:
					os.kill(pid, signal.SIGKILL)
					out['reaper_killed'] = True
				except Exception:
					pass

		try:
			if reaper_pid_path.exists():
				reaper_pid_path.unlink()
		except Exception:
			pass

		out['note'] = 'Reaper disabled; Chrome should remain open for manual inspection.'
		return out

	# keep_open == false
	try:
		if marker.exists():
			marker.unlink()
	except Exception:
		pass
	out['note'] = (
		'Marker removed. If you previously killed the reaper, automatic cleanup will only resume '
		'after Chrome is restarted via ensure_cdp_chrome.sh.'
	)
	return out


def _get_shared_state_path() -> Path:
	explicit = (os.getenv('BROWSER_USE_MCP_SHARED_STATE_PATH') or '').strip()
	if explicit:
		return Path(explicit).expanduser()
	return _default_state_root() / 'shared_state.json'

def _get_viewport_size() -> tuple[int, int]:
	w = (os.getenv('UI_VIEWPORT_WIDTH') or os.getenv('BROWSER_USE_VIEWPORT_WIDTH') or '1600').strip()
	h = (os.getenv('UI_VIEWPORT_HEIGHT') or os.getenv('BROWSER_USE_VIEWPORT_HEIGHT') or '900').strip()
	try:
		width = int(w)
		height = int(h)
	except Exception:
		return (1600, 900)
	if width <= 0 or height <= 0:
		return (1600, 900)
	return (width, height)


def _get_llm() -> ChatOpenAI:
	base_url = (os.getenv('OPENAI_BASE_URL') or os.getenv('OPENAI_API_BASE') or '').strip()
	api_key = (os.getenv('OPENAI_API_KEY') or '').strip()
	model = (
		(os.getenv('UI_VISION_MODEL') or '').strip()
		or (os.getenv('BROWSER_USE_VISION_MODEL') or '').strip()
		or (os.getenv('BROWSER_USE_LLM_MODEL') or '').strip()
		or 'gemini-3-pro-preview'
	)

	if not base_url:
		raise RuntimeError('Missing OPENAI_BASE_URL/OPENAI_API_BASE for vision model')
	if not api_key:
		raise RuntimeError('Missing OPENAI_API_KEY for vision model')

	return ChatOpenAI(model=model, api_key=api_key, base_url=base_url, temperature=0.2)


async def _pick_page(browser: Any, url_contains: str | None, state_url: str | None) -> Any:
	# Playwright Browser from chromium.connect_over_cdp has one or more contexts.
	contexts = list(getattr(browser, 'contexts', []))
	pages: list[Any] = []
	for ctx in contexts:
		pages.extend(getattr(ctx, 'pages', []))

	if not pages and contexts:
		# No pages found; create a new one.
		page = await contexts[0].new_page()
		await page.goto('about:blank')
		return page

	def score(p: Any) -> int:
		try:
			url = p.url or ''
		except Exception:
			url = ''
		s = 0
		if url_contains and url_contains in url:
			s += 10
		if state_url and state_url == url:
			s += 20
		if url and url not in {'about:blank', 'chrome://newtab/'}:
			s += 1
		return s

	pages_sorted = sorted(pages, key=score, reverse=True)
	return pages_sorted[0] if pages_sorted else pages[-1]

async def _strip_browser_use_overlays(page: Any) -> None:
	# Best-effort: remove browser-use overlays so screenshots reflect the real UI (no debug/highlight layers).
	#
	# We remove elements instead of injecting CSS so this also works on pages with strict CSP (no inline styles).
	script = r"""
	(() => {
		try {
			const selectors = [
				'#browser-use-debug-highlights',
				'[data-browser-use-highlight]',
				'[data-browser-use-interaction-highlight]',
				'[data-browser-use-coordinate-highlight]',
				'[data-browser-use-cursor]',
				'#browser-use-demo-panel',
				'#browser-use-demo-toggle',
				'#browser-use-demo-panel-style',
			];

			for (const sel of selectors) {
				try {
					document.querySelectorAll(sel).forEach((el) => {
						try { el.remove(); } catch (_) {}
					});
				} catch (_) {}
			}
		} catch (_) {}
		return true;
	})()
	"""
	try:
		await page.evaluate(script)
	except Exception:
		# Never fail ui_describe because a site blocks JS evaluation or the page is in a weird state.
		return


async def describe_ui(*, question: str | None, url_contains: str | None, full_page: bool, max_chars: int | None) -> str:
	from playwright.async_api import async_playwright

	cdp_url = _get_cdp_url()
	state_url = None
	state_path = _get_shared_state_path()
	if state_path.exists():
		try:
			state = json.loads(state_path.read_text())
			if isinstance(state, dict):
				state_url = state.get('url')
		except Exception:
			state_url = None

	start = time.time()

	async with async_playwright() as p:
		try:
			browser = await p.chromium.connect_over_cdp(cdp_url)
		except Exception as exc:
			# Common failure mode: CDP Chrome got killed/restarted after the MCP server started.
			# Try to re-run the Chrome bootstrapper and reconnect once.
			if not _looks_like_cdp_connect_error(exc):
				raise
			_ensure_cdp_chrome_ready()
			cdp_url = _get_cdp_url()
			browser = await p.chromium.connect_over_cdp(cdp_url)
		try:
			page = await _pick_page(browser, url_contains=url_contains, state_url=state_url)
			try:
				await page.bring_to_front()
			except Exception:
				pass

			# Normalize viewport for consistent screenshots.
			vw, vh = _get_viewport_size()
			try:
				await page.set_viewport_size({'width': vw, 'height': vh})
			except Exception:
				pass

			page_url = getattr(page, 'url', None) or ''
			try:
				page_title = await page.title()
			except Exception:
				page_title = ''

			await _strip_browser_use_overlays(page)

			screenshot_bytes: bytes = await page.screenshot(type='png', full_page=full_page)
		finally:
			await browser.close()

	try:
		llm = _get_llm()
	except Exception as exc:
		elapsed_ms = int((time.time() - start) * 1000)
		note = (
			'LLM not configured for ui-describe. Set OPENAI_API_BASE/OPENAI_BASE_URL + OPENAI_API_KEY '
			f'to enable screenshot-to-text. ({type(exc).__name__}: {exc})'
		)
		return f'URL: {page_url}\nTitle: {page_title}\nElapsed: {elapsed_ms}ms\n\n{note}\nScreenshot bytes: {len(screenshot_bytes)}'

	prompt_language = (os.getenv('UI_DESCRIBE_LANGUAGE') or 'de').strip().lower()
	lang_line = 'Antworte auf Deutsch.' if prompt_language.startswith('de') else 'Answer in English.'

	system = (
		'Du bist ein UI-Inspektions-Assistent. Du bekommst einen Screenshot des aktuellen Browser-UI.\n'
		'- Beschreibe nur, was sichtbar ist (keine Halluzinationen).\n'
		'- Wenn Text/Elemente nicht lesbar sind, sag das explizit.\n'
		'- Nenne sichtbare Buttons/Inputs/Fehler/Modals mit ihrem Text.\n'
		'- Beantworte die Frage, falls möglich.\n'
		f'{lang_line}\n'
	)

	user_text = 'Beschreibe den aktuellen UI-Zustand.'
	if question and question.strip():
		user_text = f'Frage zur Verifikation: {question.strip()}'

	meta = f'Meta: url={page_url} title={page_title}'
	data_uri = 'data:image/png;base64,' + base64.b64encode(screenshot_bytes).decode('ascii')

	completion = await llm.ainvoke(
		[
			SystemMessage(content=system),
			UserMessage(
				content=[
					ContentPartTextParam(text=f'{meta}\n{user_text}'),
					ContentPartImageParam(image_url=ImageURL(url=data_uri, detail='auto', media_type='image/png')),
				]
			),
		]
	)

	text = (completion.completion or '').strip()
	if max_chars and max_chars > 0 and len(text) > max_chars:
		text = text[: max_chars - 20].rstrip() + '\n…[truncated]'

	elapsed_ms = int((time.time() - start) * 1000)
	return f'URL: {page_url}\nTitle: {page_title}\nElapsed: {elapsed_ms}ms\n\n{text}'


class UIDescribeServer:
	def __init__(self):
		if not MCP_AVAILABLE:
			raise RuntimeError('MCP SDK not available (pip install mcp)')
		self.server = Server('ui-describe')
		self._setup_handlers()

	def _setup_handlers(self) -> None:
		@self.server.list_tools()
		async def handle_list_tools() -> list[types.Tool]:
			return [
				types.Tool(
					name='set_browser_keep_open',
					description='Keep the Chrome session open for manual inspection (disables the auto-cleanup reaper).',
					inputSchema={
						'type': 'object',
						'properties': {
							'keep_open': {'type': 'boolean', 'description': 'When true, keep Chrome open.'},
						},
						'required': ['keep_open'],
					},
				),
				types.Tool(
					name='ui_describe',
					description='Capture a screenshot of the current browser UI and return a TEXT description (no image returned).',
					inputSchema={
						'type': 'object',
						'properties': {
							'question': {'type': 'string', 'description': 'What should be verified in the UI?'},
							'url_contains': {
								'type': 'string',
								'description': 'Optional substring to select a specific tab by URL.',
							},
							'full_page': {
								'type': 'boolean',
								'description': 'Capture full page screenshot (may be slower).',
								'default': False,
							},
							'max_chars': {
								'type': 'integer',
								'description': 'Max characters to return for the description.',
								'default': 2000,
							},
						},
					},
				)
			]

		@self.server.call_tool()
		async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.Content]:
			args = arguments or {}
			if name == 'set_browser_keep_open':
				try:
					result = set_browser_keep_open(bool(args.get('keep_open')))
					return [types.TextContent(type='text', text=json.dumps(result, ensure_ascii=False, indent=2))]
				except Exception as exc:
					logger.error('set_browser_keep_open failed', exc_info=True)
					return [types.TextContent(type='text', text=f'Error: {type(exc).__name__}: {exc}')]

			if name != 'ui_describe':
				return [types.TextContent(type='text', text=f'Error: Unknown tool: {name}')]

			try:
				result = await describe_ui(
					question=args.get('question'),
					url_contains=args.get('url_contains'),
					full_page=bool(args.get('full_page', False)),
					max_chars=int(args.get('max_chars', 2000)) if args.get('max_chars') is not None else None,
				)
				return [types.TextContent(type='text', text=result)]
			except Exception as exc:
				logger.error('ui_describe failed', exc_info=True)
				return [types.TextContent(type='text', text=f'Error: {type(exc).__name__}: {exc}')]

	async def run(self) -> None:
		async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
			await self.server.run(
				read_stream,
				write_stream,
				InitializationOptions(
					server_name='ui-describe',
					server_version='0.1.0',
					capabilities=self.server.get_capabilities(
						notification_options=NotificationOptions(),
						experimental_capabilities={},
					),
				),
			)


async def main() -> None:
	server = UIDescribeServer()
	await server.run()


if __name__ == '__main__':
	asyncio.run(main())
