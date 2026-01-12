import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

os.environ.setdefault('NODE_NO_WARNINGS', '1')

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
logger = logging.getLogger('chrome-devtools-mcp')

# Prevent MCP SDK logs from polluting stdout (stdio transport).
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
	return (os.getenv('DEVTOOLS_CDP_URL') or os.getenv('BROWSER_USE_CDP_URL') or 'http://127.0.0.1:9222').strip()


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


def _get_shared_state_path() -> Path:
	explicit = (os.getenv('BROWSER_USE_MCP_SHARED_STATE_PATH') or '').strip()
	if explicit:
		return Path(explicit).expanduser()
	return _default_state_root() / 'shared_state.json'


def _get_data_dir() -> Path:
	explicit = (os.getenv('DEVTOOLS_DATA_DIR') or '').strip()
	if explicit:
		p = Path(explicit).expanduser()
		p.mkdir(parents=True, exist_ok=True)
		return p
	state_path = _get_shared_state_path()
	p = state_path.parent / 'devtools'
	p.mkdir(parents=True, exist_ok=True)
	return p


def _json_dumps(obj: Any) -> str:
	return json.dumps(obj, ensure_ascii=False, indent=2, default=lambda o: repr(o))


def _truncate_text(s: str, max_chars: int) -> str:
	if max_chars <= 0:
		return s
	if len(s) <= max_chars:
		return s
	return s[: max(0, max_chars - 20)].rstrip() + '\n…[truncated]'


def _coerce_int(val: Any, default: int) -> int:
	try:
		return int(val)
	except Exception:
		return default


def _coerce_float(val: Any, default: float) -> float:
	try:
		return float(val)
	except Exception:
		return default


@dataclass
class PageEntry:
	page_id: str
	page: Any
	cdp: Any


class ChromeDevtoolsRuntime:
	def __init__(self) -> None:
		self.cdp_url = _get_cdp_url()
		self.shared_state_path = _get_shared_state_path()
		self.data_dir = _get_data_dir()

		self._playwright = None
		self._browser = None
		self._browser_cdp = None
		self._watch_task: asyncio.Task[None] | None = None
		self._stop_event = asyncio.Event()

		self._page_counter = 0
		self._pages_by_obj: dict[int, PageEntry] = {}
		self._pages_by_id: dict[str, PageEntry] = {}

		self._network_order: list[str] = []
		self._network: dict[str, dict[str, Any]] = {}
		self._network_limit = _coerce_int(os.getenv('DEVTOOLS_NETWORK_MAX', '2000'), 2000)

		self._console: dict[str, list[dict[str, Any]]] = {}
		self._console_limit = _coerce_int(os.getenv('DEVTOOLS_CONSOLE_MAX', '2000'), 2000)

		self._trace_active = False
		self._trace_id: str | None = None
		self._trace_path: Path | None = None
		self._trace_started_at_unix: float | None = None

	def _session_dir(self) -> Path:
		return self.shared_state_path.parent

	def _keep_open_marker_path(self) -> Path:
		return self._session_dir() / 'keep-browser-open.flag'

	def _reaper_pid_path(self) -> Path:
		return self._session_dir() / 'chrome.reaper.pid'

	@staticmethod
	def _pid_alive(pid: int) -> bool:
		try:
			os.kill(pid, 0)
			return True
		except Exception:
			return False

	@staticmethod
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

	def set_browser_keep_open(self, *, keep_open: bool) -> dict[str, Any]:
		"""Best-effort toggle for keeping the session Chrome alive after the owner process exits.

		In session mode, `ensure_cdp_chrome.sh` spawns a reaper process that terminates Chrome once the
		owner PID ends. This tool can:
		- create/remove a marker file checked by the launcher to suppress future reapers
		- kill the currently running reaper (so the browser stays open now)
		"""

		session_dir = self._session_dir()
		marker = self._keep_open_marker_path()
		reaper_pid_path = self._reaper_pid_path()

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
			try:
				marker.write_text(f'enabled_at_unix={time.time()}\n', encoding='utf-8')
			except Exception as exc:
				raise RuntimeError(f'Failed to write marker file: {marker}: {type(exc).__name__}: {exc}') from exc

			pid = self._read_pid(reaper_pid_path) if reaper_pid_path.exists() else None
			out['reaper_pid_before'] = pid

			if pid and self._pid_alive(pid):
				try:
					os.kill(pid, signal.SIGTERM)
					out['reaper_killed'] = True
				except Exception:
					out['reaper_killed'] = False

				# Give it a moment; if it lingers, SIGKILL.
				for _ in range(10):
					if not self._pid_alive(pid):
						break
					time.sleep(0.05)
				if self._pid_alive(pid):
					try:
						os.kill(pid, signal.SIGKILL)
						out['reaper_killed'] = True
					except Exception:
						pass

			# Best-effort: remove PID file so future logic doesn't assume a reaper is active.
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

	async def start(self) -> None:
		from playwright.async_api import async_playwright

		self._playwright = await async_playwright().start()
		self.cdp_url = _get_cdp_url()
		try:
			self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
		except Exception as exc:
			# Common failure mode: CDP Chrome got killed/restarted after the MCP server started.
			# Try to re-run the Chrome bootstrapper and reconnect once.
			if not _looks_like_cdp_connect_error(exc):
				raise
			_ensure_cdp_chrome_ready()
			self.cdp_url = _get_cdp_url()
			self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)

		try:
			self._browser_cdp = await self._browser.new_browser_cdp_session()
			try:
				await self._browser_cdp.send('Network.enable', {})
			except Exception:
				pass
		except Exception:
			self._browser_cdp = None

		await self._ensure_attached_pages()
		self._watch_task = asyncio.create_task(self._watch_pages_loop())

	async def close(self) -> None:
		self._stop_event.set()
		if self._watch_task:
			self._watch_task.cancel()
			try:
				await self._watch_task
			except asyncio.CancelledError:
				pass
			except Exception:
				pass
		if self._browser:
			try:
				await self._browser.close()
			except Exception:
				pass
		if self._playwright:
			try:
				await self._playwright.stop()
			except Exception:
				pass

	async def _watch_pages_loop(self) -> None:
		while not self._stop_event.is_set():
			try:
				await self._ensure_attached_pages()
			except Exception:
				logger.debug('watch loop failed', exc_info=True)
			await asyncio.sleep(1.0)

	def _state_url(self) -> str | None:
		try:
			if not self.shared_state_path.exists():
				return None
			raw = self.shared_state_path.read_text(encoding='utf-8')
			obj = json.loads(raw)
			if isinstance(obj, dict):
				url = obj.get('url')
				if isinstance(url, str) and url.strip():
					return url.strip()
		except Exception:
			return None
		return None

	async def _ensure_attached_pages(self) -> None:
		if not self._browser:
			return
		contexts = list(getattr(self._browser, 'contexts', []))
		for ctx in contexts:
			pages = list(getattr(ctx, 'pages', []))
			for page in pages:
				try:
					if getattr(page, 'is_closed', None) and page.is_closed():
						continue
				except Exception:
					continue
				key = id(page)
				if key in self._pages_by_obj:
					continue
				await self._attach_page(page)

	async def _attach_page(self, page: Any) -> None:
		if not self._browser:
			return

		try:
			ctx = page.context
			cdp = await ctx.new_cdp_session(page)
		except Exception:
			return

		self._page_counter += 1
		page_id = f'page-{self._page_counter}'
		entry = PageEntry(page_id=page_id, page=page, cdp=cdp)

		self._pages_by_obj[id(page)] = entry
		self._pages_by_id[page_id] = entry

		try:
			await cdp.send('Network.enable', {})
		except Exception:
			pass
		try:
			await cdp.send('Runtime.enable', {})
		except Exception:
			pass

		self._console.setdefault(page_id, [])

		def on_request_will_be_sent(params: dict[str, Any]) -> None:
			self._handle_request_will_be_sent(page_id, params)

		def on_response_received(params: dict[str, Any]) -> None:
			self._handle_response_received(page_id, params)

		def on_loading_finished(params: dict[str, Any]) -> None:
			self._handle_loading_finished(page_id, params)

		def on_loading_failed(params: dict[str, Any]) -> None:
			self._handle_loading_failed(page_id, params)

		def on_console(params: dict[str, Any]) -> None:
			self._handle_console(page_id, params)

		def on_exception(params: dict[str, Any]) -> None:
			self._handle_exception(page_id, params)

		try:
			cdp.on('Network.requestWillBeSent', on_request_will_be_sent)
			cdp.on('Network.responseReceived', on_response_received)
			cdp.on('Network.loadingFinished', on_loading_finished)
			cdp.on('Network.loadingFailed', on_loading_failed)
			cdp.on('Runtime.consoleAPICalled', on_console)
			cdp.on('Runtime.exceptionThrown', on_exception)
		except Exception:
			logger.debug('failed to subscribe to CDP events', exc_info=True)

	def _make_request_key(self, page_id: str, request_id: str) -> str:
		return f'{page_id}:{request_id}'

	def _evict_if_needed(self) -> None:
		limit = max(0, self._network_limit)
		if limit == 0:
			self._network.clear()
			self._network_order.clear()
			return
		while len(self._network_order) > limit:
			old = self._network_order.pop(0)
			self._network.pop(old, None)

	def _handle_request_will_be_sent(self, page_id: str, params: dict[str, Any]) -> None:
		req_id = str(params.get('requestId') or '')
		if not req_id:
			return
		req = params.get('request') or {}
		key = self._make_request_key(page_id, req_id)
		obj: dict[str, Any] = self._network.get(key) or {
			'id': key,
			'page_id': page_id,
			'request_id': req_id,
			'start_time_unix': time.time(),
		}
		obj.update(
			{
				'url': req.get('url'),
				'method': req.get('method'),
				'resource_type': params.get('type'),
				'initiator': params.get('initiator'),
				'request_headers': req.get('headers'),
				'post_data': req.get('postData'),
				'wall_time': params.get('wallTime'),
				'timestamp': params.get('timestamp'),
				'document_url': params.get('documentURL'),
			}
		)
		self._network[key] = obj
		self._network_order.append(key)
		self._evict_if_needed()

	def _handle_response_received(self, page_id: str, params: dict[str, Any]) -> None:
		req_id = str(params.get('requestId') or '')
		if not req_id:
			return
		key = self._make_request_key(page_id, req_id)
		resp = params.get('response') or {}
		obj: dict[str, Any] = self._network.get(key) or {
			'id': key,
			'page_id': page_id,
			'request_id': req_id,
			'start_time_unix': time.time(),
		}
		obj.update(
			{
				'status': resp.get('status'),
				'status_text': resp.get('statusText'),
				'response_headers': resp.get('headers'),
				'mime_type': resp.get('mimeType'),
				'protocol': resp.get('protocol'),
				'remote_ip': resp.get('remoteIPAddress'),
				'remote_port': resp.get('remotePort'),
				'encoded_data_length': resp.get('encodedDataLength'),
				'from_disk_cache': resp.get('fromDiskCache'),
				'from_service_worker': resp.get('fromServiceWorker'),
				'response_url': resp.get('url'),
				'timestamp_response': params.get('timestamp'),
				'resource_type': params.get('type') or obj.get('resource_type'),
			}
		)
		self._network[key] = obj

	def _handle_loading_finished(self, page_id: str, params: dict[str, Any]) -> None:
		req_id = str(params.get('requestId') or '')
		if not req_id:
			return
		key = self._make_request_key(page_id, req_id)
		obj = self._network.get(key)
		if not obj:
			return
		obj['end_time_unix'] = time.time()
		obj['encoded_data_length_finished'] = params.get('encodedDataLength')
		obj['timestamp_finished'] = params.get('timestamp')

	def _handle_loading_failed(self, page_id: str, params: dict[str, Any]) -> None:
		req_id = str(params.get('requestId') or '')
		if not req_id:
			return
		key = self._make_request_key(page_id, req_id)
		obj = self._network.get(key)
		if not obj:
			obj = {'id': key, 'page_id': page_id, 'request_id': req_id, 'start_time_unix': time.time()}
			self._network[key] = obj
			self._network_order.append(key)
			self._evict_if_needed()
		obj['end_time_unix'] = time.time()
		obj['error_text'] = params.get('errorText')
		obj['canceled'] = params.get('canceled')
		obj['blocked_reason'] = params.get('blockedReason')
		obj['timestamp_failed'] = params.get('timestamp')

	def _append_console(self, page_id: str, msg: dict[str, Any]) -> None:
		arr = self._console.setdefault(page_id, [])
		arr.append(msg)
		limit = max(0, self._console_limit)
		if limit == 0:
			arr.clear()
			return
		if len(arr) > limit:
			del arr[: len(arr) - limit]

	def _handle_console(self, page_id: str, params: dict[str, Any]) -> None:
		args = params.get('args') or []
		parts: list[str] = []
		for a in args:
			if isinstance(a, dict):
				if 'value' in a:
					parts.append(str(a.get('value')))
				elif 'description' in a:
					parts.append(str(a.get('description')))
				else:
					parts.append(str(a))
			else:
				parts.append(str(a))
		self._append_console(
			page_id,
			{
				'type': params.get('type'),
				'text': ' '.join(parts).strip(),
				'timestamp': params.get('timestamp'),
				'stack_trace': params.get('stackTrace'),
				'time_unix': time.time(),
			},
		)

	def _handle_exception(self, page_id: str, params: dict[str, Any]) -> None:
		exc = params.get('exceptionDetails') or {}
		text = exc.get('text') or 'Exception'
		exc_obj = exc.get('exception') or {}
		desc = exc_obj.get('description') or ''
		line = exc.get('lineNumber')
		col = exc.get('columnNumber')
		url = exc.get('url')
		msg = f'{text}: {desc}'.strip()
		meta = []
		if url:
			meta.append(str(url))
		if line is not None:
			meta.append(f'line={line}')
		if col is not None:
			meta.append(f'col={col}')
		if meta:
			msg += ' (' + ', '.join(meta) + ')'
		self._append_console(page_id, {'type': 'exception', 'text': msg, 'time_unix': time.time(), 'raw': exc})

	async def _pick_page(self, url_contains: str | None) -> PageEntry:
		await self._ensure_attached_pages()

		entries = list(self._pages_by_id.values())
		if not entries and self._browser and getattr(self._browser, 'contexts', None):
			ctxs = list(self._browser.contexts)
			if ctxs:
				page = await ctxs[0].new_page()
				await page.goto('about:blank')
				await self._attach_page(page)
				entries = list(self._pages_by_id.values())

		if not entries:
			raise RuntimeError('No pages available in the connected Chrome session')

		state_url = self._state_url()

		def score(e: PageEntry) -> int:
			try:
				url = e.page.url or ''
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

		return sorted(entries, key=score, reverse=True)[0]

	async def list_network_requests(self, *, url_contains: str | None, limit: int, include_headers: bool) -> dict[str, Any]:
		entry = await self._pick_page(url_contains)
		page_id = entry.page_id
		items = [self._network[rid] for rid in self._network_order if self._network.get(rid, {}).get('page_id') == page_id]
		if limit > 0:
			items = items[-limit:]

		out_items: list[dict[str, Any]] = []
		for obj in items:
			# Default to a compact representation to avoid huge tool outputs (initiator stacks can be enormous).
			compact: dict[str, Any] = {}
			for key in (
				'id',
				'url',
				'method',
				'resource_type',
				'status',
				'mime_type',
				'protocol',
				'encoded_data_length_finished',
				'encoded_data_length',
				'from_disk_cache',
				'from_service_worker',
				'error_text',
				'blocked_reason',
				'canceled',
			):
				if key in obj:
					compact[key] = obj.get(key)
			# Optionally include headers/post bodies for deep debugging.
			if include_headers:
				for key in ('request_headers', 'response_headers', 'post_data'):
					if key in obj:
						compact[key] = obj.get(key)
			out_items.append(compact)

		try:
			title = await entry.page.title()
		except Exception:
			title = ''
		return {
			'page': {'page_id': page_id, 'url': getattr(entry.page, 'url', ''), 'title': title},
			'count': len(out_items),
			'requests': out_items,
		}

	async def summarize_network_requests(self, *, url_contains: str | None, limit: int, top_hosts: int) -> dict[str, Any]:
		entry = await self._pick_page(url_contains)
		page_id = entry.page_id
		items = [self._network[rid] for rid in self._network_order if self._network.get(rid, {}).get('page_id') == page_id]
		if limit > 0:
			items = items[-limit:]

		by_host: dict[str, int] = {}
		by_type: dict[str, int] = {}
		by_status: dict[str, int] = {}
		errors: dict[str, int] = {}
		from_disk_cache = 0
		from_service_worker = 0
		total_encoded_bytes = 0

		for obj in items:
			url = str(obj.get('url') or '')
			host = ''
			try:
				host = urlparse(url).hostname or ''
			except Exception:
				host = ''
			if host:
				by_host[host] = by_host.get(host, 0) + 1

			typ = str(obj.get('resource_type') or '')
			if typ:
				by_type[typ] = by_type.get(typ, 0) + 1

			status = obj.get('status')
			if status is not None:
				by_status[str(status)] = by_status.get(str(status), 0) + 1

			if obj.get('from_disk_cache'):
				from_disk_cache += 1
			if obj.get('from_service_worker'):
				from_service_worker += 1

			enc = obj.get('encoded_data_length_finished')
			if enc is None:
				enc = obj.get('encoded_data_length')
			try:
				if enc is not None:
					total_encoded_bytes += int(enc)
			except Exception:
				pass

			err = str(obj.get('error_text') or '').strip()
			if err:
				errors[err] = errors.get(err, 0) + 1

		top = sorted(by_host.items(), key=lambda kv: kv[1], reverse=True)[: max(0, int(top_hosts or 0))]

		try:
			title = await entry.page.title()
		except Exception:
			title = ''

		return {
			'page': {'page_id': page_id, 'url': getattr(entry.page, 'url', ''), 'title': title},
			'count': len(items),
			'total_encoded_bytes_approx': total_encoded_bytes,
			'cache': {'from_disk_cache': from_disk_cache, 'from_service_worker': from_service_worker},
			'top_hosts': [{'host': host, 'count': count} for host, count in top],
			'by_type': dict(sorted(by_type.items(), key=lambda kv: kv[1], reverse=True)),
			'by_status': dict(sorted(by_status.items(), key=lambda kv: kv[1], reverse=True)),
			'errors': dict(sorted(errors.items(), key=lambda kv: kv[1], reverse=True)),
		}

	async def get_network_request(self, *, request_id: str, include_response_body: bool, max_body_chars: int) -> dict[str, Any]:
		obj = self._network.get(request_id)
		if not obj:
			raise RuntimeError(f'Unknown request_id: {request_id}')

		page_id = obj.get('page_id')
		entry = self._pages_by_id.get(str(page_id))
		if not entry:
			raise RuntimeError(f'Page for request is not available anymore: {page_id}')

		out = dict(obj)
		if include_response_body:
			try:
				body = await entry.cdp.send('Network.getResponseBody', {'requestId': obj.get('request_id')})
				if isinstance(body, dict):
					text = str(body.get('body') or '')
					out['response_body_base64_encoded'] = bool(body.get('base64Encoded'))
					out['response_body'] = _truncate_text(text, max_body_chars)
				else:
					out['response_body'] = _truncate_text(str(body), max_body_chars)
			except Exception as exc:
				out['response_body_error'] = f'{type(exc).__name__}: {exc}'

		return out

	async def list_console_messages(self, *, url_contains: str | None, limit: int) -> dict[str, Any]:
		entry = await self._pick_page(url_contains)
		page_id = entry.page_id
		msgs = list(self._console.get(page_id) or [])
		if limit > 0:
			msgs = msgs[-limit:]
		try:
			title = await entry.page.title()
		except Exception:
			title = ''
		return {
			'page': {'page_id': page_id, 'url': getattr(entry.page, 'url', ''), 'title': title},
			'count': len(msgs),
			'messages': msgs,
		}

	async def evaluate_script(self, *, script: str, url_contains: str | None) -> dict[str, Any]:
		if not script.strip():
			raise RuntimeError('script must be a non-empty string')
		entry = await self._pick_page(url_contains)
		try:
			result = await entry.page.evaluate(script)
		except Exception as exc:
			raise RuntimeError(f'JS evaluate failed: {type(exc).__name__}: {exc}') from exc
		return {'page_id': entry.page_id, 'url': getattr(entry.page, 'url', ''), 'result': result}

	async def performance_start_trace(self, *, categories: list[str] | None, options: str | None) -> dict[str, Any]:
		if not self._browser_cdp:
			raise RuntimeError('Browser-level CDP session is not available')
		if self._trace_active:
			raise RuntimeError('Tracing already active')

		trace_id = f'trace-{int(time.time())}'
		cats = categories or [
			'devtools.timeline',
			'v8.execute',
			'disabled-by-default-devtools.timeline',
			'disabled-by-default-devtools.timeline.frame',
			'disabled-by-default-devtools.timeline.stack',
			'disabled-by-default-v8.cpu_profiler',
		]

		await self._browser_cdp.send(
			'Tracing.start',
			{
				'categories': ','.join(cats),
				'options': (options or 'sampling-frequency=10000'),
				'transferMode': 'ReturnAsStream',
			},
		)

		self._trace_active = True
		self._trace_id = trace_id
		self._trace_path = None
		self._trace_started_at_unix = time.time()

		return {
			'trace_id': trace_id,
			'started_at_unix': self._trace_started_at_unix,
			'categories': cats,
			'options': options or 'sampling-frequency=10000',
		}

	async def performance_stop_trace(self, *, timeout_seconds: float) -> dict[str, Any]:
		if not self._browser_cdp:
			raise RuntimeError('Browser-level CDP session is not available')
		if not self._trace_active:
			raise RuntimeError('Tracing is not active')
		if not self._trace_id:
			raise RuntimeError('Tracing state corrupted (missing trace_id)')

		loop = asyncio.get_running_loop()
		done: asyncio.Future[dict[str, Any]] = loop.create_future()

		def on_complete(params: dict[str, Any]) -> None:
			if not done.done():
				done.set_result(params)

		self._browser_cdp.once('Tracing.tracingComplete', on_complete)
		await self._browser_cdp.send('Tracing.end', {})

		params = await asyncio.wait_for(done, timeout=timeout_seconds)
		stream = (params or {}).get('stream')
		if not stream:
			raise RuntimeError('Tracing completed but no stream was returned')

		trace_path = self.data_dir / f'{self._trace_id}.json'
		chunks: list[str] = []
		while True:
			resp = await self._browser_cdp.send('IO.read', {'handle': stream})
			if not isinstance(resp, dict):
				break
			data = resp.get('data') or ''
			if data:
				chunks.append(str(data))
			if resp.get('eof'):
				break
		try:
			await self._browser_cdp.send('IO.close', {'handle': stream})
		except Exception:
			pass

		raw = ''.join(chunks)
		trace_path.write_text(raw, encoding='utf-8')

		self._trace_active = False
		self._trace_path = trace_path

		return {
			'trace_id': self._trace_id,
			'started_at_unix': self._trace_started_at_unix,
			'stopped_at_unix': time.time(),
			'trace_path': str(trace_path),
			'bytes': trace_path.stat().st_size if trace_path.exists() else None,
		}

	def _analyze_trace_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
		# Minimal heuristics: long tasks + frequent layout/paint.
		# Some trace payloads include events with ts=0; ignore those for duration.
		seen_ts = [
			e.get('ts')
			for e in events
			if isinstance(e, dict) and isinstance(e.get('ts'), (int, float)) and e.get('ts')
		]
		if seen_ts:
			start_us = min(seen_ts)
			end_us = max(seen_ts)
			duration_ms = (end_us - start_us) / 1000.0
		else:
			duration_ms = None

		long_tasks: list[dict[str, Any]] = []
		layout_events = 0
		paint_events = 0

		for e in events:
			if not isinstance(e, dict):
				continue
			name = str(e.get('name') or '')
			ph = str(e.get('ph') or '')
			dur = e.get('dur')
			if ph == 'X' and isinstance(dur, (int, float)):
				dur_ms = dur / 1000.0
				if dur_ms >= 50.0:
					long_tasks.append({'name': name, 'dur_ms': dur_ms, 'cat': e.get('cat')})
			if name in {'Layout', 'UpdateLayoutTree'}:
				layout_events += 1
			if name in {'Paint', 'CompositeLayers', 'Rasterize', 'UpdateLayerTree'}:
				paint_events += 1

		long_tasks.sort(key=lambda x: x.get('dur_ms') or 0.0, reverse=True)

		return {
			'duration_ms': duration_ms,
			'long_tasks_count': len(long_tasks),
			'long_tasks_top': long_tasks[:10],
			'layout_events': layout_events,
			'paint_events': paint_events,
		}

	async def performance_analyze_insight(self, *, trace_path: str | None, max_chars: int) -> str:
		path: Path | None = None
		if trace_path and trace_path.strip():
			path = Path(trace_path).expanduser()
		elif self._trace_path:
			path = self._trace_path

		if not path:
			raise RuntimeError('No trace_path provided and no previous trace available')
		if not path.exists():
			raise RuntimeError(f'Trace file not found: {path}')

		raw = path.read_text(encoding='utf-8', errors='replace')
		try:
			obj = json.loads(raw)
		except Exception as exc:
			raise RuntimeError(f'Could not parse trace JSON: {type(exc).__name__}: {exc}') from exc

		events = obj.get('traceEvents') if isinstance(obj, dict) else None
		if not isinstance(events, list):
			raise RuntimeError('Trace JSON does not contain traceEvents[]')

		stats = self._analyze_trace_events(events)

		lines = []
		lines.append(f'Trace: {path}')
		if stats.get('duration_ms') is not None:
			lines.append(f'Duration: {stats.get("duration_ms"):.1f}ms')
		lines.append(f'Long tasks (>=50ms): {stats.get("long_tasks_count")}')
		lines.append(f'Layout events: {stats.get("layout_events")}, Paint/Composite events: {stats.get("paint_events")}')
		if stats.get('long_tasks_top'):
			lines.append('')
			lines.append('Top long tasks:')
			for t in stats['long_tasks_top'][:5]:
				lines.append(f'- {t.get("name")}: {t.get("dur_ms"):.1f}ms ({t.get("cat")})')
		lines.append('')
		lines.append('Heuristics:')
		if (stats.get('long_tasks_count') or 0) > 0:
			lines.append('- Reduce long main-thread tasks (split work, debounce handlers, avoid heavy sync JS).')
		if (stats.get('layout_events') or 0) > 20:
			lines.append('- Many layout events: batch DOM reads/writes, avoid layout thrashing, use CSS containment where possible.')
		if (stats.get('paint_events') or 0) > 20:
			lines.append('- Many paint/composite events: check large repaints, expensive effects, and overdraw.')
		if len(lines) <= 4:
			lines.append('- No obvious hotspots detected by simple heuristics; inspect flame chart for detailed breakdown.')

		return _truncate_text('\n'.join(lines).strip(), max_chars)


class ChromeDevtoolsMCPServer:
	def __init__(self) -> None:
		if not MCP_AVAILABLE:
			raise RuntimeError('MCP SDK not available (pip install mcp)')
		self.server = Server('chrome-devtools')
		self.runtime = ChromeDevtoolsRuntime()
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
					name='list_network_requests',
					description='List captured network requests for the selected tab (DevTools Network-like).',
					inputSchema={
						'type': 'object',
						'properties': {
							'url_contains': {'type': 'string', 'description': 'Optional substring to select a tab by URL.'},
							'limit': {'type': 'integer', 'description': 'Max number of requests to return.', 'default': 200},
							'include_headers': {'type': 'boolean', 'description': 'Include request/response headers.', 'default': False},
						},
					},
				),
				types.Tool(
					name='summarize_network_requests',
					description='Return a compact summary of captured network requests (safe for token limits).',
					inputSchema={
						'type': 'object',
						'properties': {
							'url_contains': {'type': 'string', 'description': 'Optional substring to select a tab by URL.'},
							'limit': {'type': 'integer', 'description': 'How many of the most recent requests to summarize.', 'default': 400},
							'top_hosts': {'type': 'integer', 'description': 'How many hostnames to return.', 'default': 12},
						},
					},
				),
				types.Tool(
					name='get_network_request',
					description='Get details for a single network request by id (from list_network_requests).',
					inputSchema={
						'type': 'object',
						'properties': {
							'request_id': {'type': 'string', 'description': 'The id field from list_network_requests.'},
							'include_response_body': {
								'type': 'boolean',
								'description': 'Try to fetch response body via CDP (may fail for some requests).',
								'default': False,
							},
							'max_body_chars': {'type': 'integer', 'description': 'Max chars for response body.', 'default': 5000},
						},
						'required': ['request_id'],
					},
				),
				types.Tool(
					name='list_console_messages',
					description='List captured console messages for the selected tab.',
					inputSchema={
						'type': 'object',
						'properties': {
							'url_contains': {'type': 'string', 'description': 'Optional substring to select a tab by URL.'},
							'limit': {'type': 'integer', 'description': 'Max number of console messages to return.', 'default': 200},
						},
					},
				),
				types.Tool(
					name='evaluate_script',
					description='Evaluate JavaScript in the selected tab and return the result.',
					inputSchema={
						'type': 'object',
						'properties': {
							'url_contains': {'type': 'string', 'description': 'Optional substring to select a tab by URL.'},
							'script': {'type': 'string', 'description': 'JavaScript expression to evaluate.'},
						},
						'required': ['script'],
					},
				),
				types.Tool(
					name='performance_start_trace',
					description='Start a Chrome performance trace (DevTools Performance recording).',
					inputSchema={
						'type': 'object',
						'properties': {
							'categories': {
								'type': 'array',
								'items': {'type': 'string'},
								'description': 'Optional CDP tracing categories.',
							},
							'options': {'type': 'string', 'description': 'Optional CDP tracing options.'},
						},
					},
				),
				types.Tool(
					name='performance_stop_trace',
					description='Stop the current performance trace and write it to a JSON file (returned as path).',
					inputSchema={
						'type': 'object',
						'properties': {
							'timeout_seconds': {'type': 'number', 'description': 'Timeout for trace finalization.', 'default': 60},
						},
					},
				),
				types.Tool(
					name='performance_analyze_insight',
					description='Analyze a performance trace JSON and return heuristic insights.',
					inputSchema={
						'type': 'object',
						'properties': {
							'trace_path': {'type': 'string', 'description': 'Path returned from performance_stop_trace.'},
							'max_chars': {'type': 'integer', 'description': 'Max characters to return.', 'default': 3000},
						},
					},
				),
			]

		@self.server.call_tool()
		async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.Content]:
			args = arguments or {}
			try:
				if name == 'set_browser_keep_open':
					result = self.runtime.set_browser_keep_open(keep_open=bool(args.get('keep_open')))
					return [types.TextContent(type='text', text=_json_dumps(result))]
				if name == 'list_network_requests':
					result = await self.runtime.list_network_requests(
						url_contains=args.get('url_contains'),
						limit=_coerce_int(args.get('limit'), 200),
						include_headers=bool(args.get('include_headers', False)),
					)
					return [types.TextContent(type='text', text=_json_dumps(result))]
				if name == 'summarize_network_requests':
					result = await self.runtime.summarize_network_requests(
						url_contains=args.get('url_contains'),
						limit=_coerce_int(args.get('limit'), 400),
						top_hosts=_coerce_int(args.get('top_hosts'), 12),
					)
					return [types.TextContent(type='text', text=_json_dumps(result))]
				if name == 'get_network_request':
					result = await self.runtime.get_network_request(
						request_id=str(args.get('request_id') or ''),
						include_response_body=bool(args.get('include_response_body', False)),
						max_body_chars=_coerce_int(args.get('max_body_chars'), 5000),
					)
					return [types.TextContent(type='text', text=_json_dumps(result))]
				if name == 'list_console_messages':
					result = await self.runtime.list_console_messages(
						url_contains=args.get('url_contains'),
						limit=_coerce_int(args.get('limit'), 200),
					)
					return [types.TextContent(type='text', text=_json_dumps(result))]
				if name == 'evaluate_script':
					result = await self.runtime.evaluate_script(
						script=str(args.get('script') or ''),
						url_contains=args.get('url_contains'),
					)
					return [types.TextContent(type='text', text=_json_dumps(result))]
				if name == 'performance_start_trace':
					result = await self.runtime.performance_start_trace(
						categories=list(args.get('categories') or []) if args.get('categories') is not None else None,
						options=(str(args.get('options')) if args.get('options') is not None else None),
					)
					return [types.TextContent(type='text', text=_json_dumps(result))]
				if name == 'performance_stop_trace':
					result = await self.runtime.performance_stop_trace(
						timeout_seconds=_coerce_float(args.get('timeout_seconds'), 60.0),
					)
					return [types.TextContent(type='text', text=_json_dumps(result))]
				if name == 'performance_analyze_insight':
					result = await self.runtime.performance_analyze_insight(
						trace_path=(str(args.get('trace_path')) if args.get('trace_path') is not None else None),
						max_chars=_coerce_int(args.get('max_chars'), 3000),
					)
					return [types.TextContent(type='text', text=result)]

				return [types.TextContent(type='text', text=f'Error: Unknown tool: {name}')]
			except Exception as exc:
				logger.error('tool failed: %s', name, exc_info=True)
				return [types.TextContent(type='text', text=f'Error: {type(exc).__name__}: {exc}')]

	async def run(self) -> None:
		await self.runtime.start()
		try:
			async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
				await self.server.run(
					read_stream,
					write_stream,
					InitializationOptions(
						server_name='chrome-devtools',
						server_version='0.1.0',
						capabilities=self.server.get_capabilities(
							notification_options=NotificationOptions(),
							experimental_capabilities={},
						),
					),
				)
		finally:
			await self.runtime.close()


async def main() -> None:
	server = ChromeDevtoolsMCPServer()
	await server.run()


if __name__ == '__main__':
	asyncio.run(main())
