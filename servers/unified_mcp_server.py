import asyncio
import base64
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

os.environ.setdefault('NODE_NO_WARNINGS', '1')

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(_REPO_ROOT))

from mcp_plus.stdio_client import MCPStdioClient
from _common import repo_root as _repo_root_common

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
logger = logging.getLogger('mcp-plus-unified')

# Prevent MCP SDK logs from polluting stdout (stdio transport).
logging.getLogger('mcp').setLevel(logging.ERROR)
logging.getLogger('mcp').propagate = False


def _env_bool(name: str, default: bool = False) -> bool:
	val = (os.getenv(name) or '').strip().lower()
	if not val:
		return default
	return val in {'1', 'true', 'yes', 'y', 'on'}


def _repo_root() -> Path:
	return _repo_root_common()


def _content_from_dict(obj: Any) -> types.Content:
	if not isinstance(obj, dict):
		return types.TextContent(type='text', text=json.dumps(obj, ensure_ascii=False))
	kind = (obj.get('type') or '').strip()
	if kind == 'text':
		return types.TextContent(type='text', text=str(obj.get('text') or ''))
	if kind == 'image':
		return types.ImageContent(
			type='image',
			data=str(obj.get('data') or ''),
			mimeType=str(obj.get('mimeType') or 'image/png'),
		)
	# Fallback: preserve raw.
	return types.TextContent(type='text', text=json.dumps(obj, ensure_ascii=False))


@dataclass
class _RoutedTool:
	child: str
	tool: str


class _ChildServer:
	def __init__(self, *, name: str, command: list[str], cwd: str | None = None) -> None:
		self.name = name
		self.command = command
		self.cwd = cwd
		self._client: MCPStdioClient | None = None
		self._lock = threading.Lock()

	def start(self) -> None:
		with self._lock:
			if self._client is not None:
				return
			client = MCPStdioClient(name=self.name, command=self.command, env={}, cwd=self.cwd)
			client.start()
			client.initialize()
			self._client = client

	def close(self) -> None:
		with self._lock:
			if self._client is None:
				return
			try:
				self._client.close()
			finally:
				self._client = None

	def request(self, method: str, params: dict[str, Any] | None, *, timeout_s: float = 30.0) -> dict[str, Any]:
		with self._lock:
			if self._client is None:
				raise RuntimeError(f'Child {self.name} not started')
			return self._client.request(method, params, timeout_s=timeout_s)


class UnifiedMCPServer:
	def __init__(self) -> None:
		if not MCP_AVAILABLE:
			raise RuntimeError('mcp python package not available in this interpreter')

		self.server = Server('mcp-plus')
		self._repo_root = _repo_root()

		self._children: dict[str, _ChildServer] = {}
		self._tool_routes: dict[str, _RoutedTool] = {}
		self._internal_handlers: dict[str, Any] = {}
		self._internal_tools: list[types.Tool] = []
		self._tools_cache: list[types.Tool] | None = None
		self._tools_lock = asyncio.Lock()

		self._init_children()
		self._init_internal_tools()
		self._register_handlers()
		self._install_signal_handlers()

	def _context7_api_key(self) -> str:
		return (os.getenv('CONTEXT7_API_KEY') or os.getenv('CONTEXT7_API_TOKEN') or '').strip()

	def _context7_base_url(self) -> str:
		return (os.getenv('CONTEXT7_BASE_URL') or 'https://context7.com').strip().rstrip('/')

	def _context7_headers(self) -> dict[str, str]:
		api_key = self._context7_api_key()
		if not api_key:
			return {}
		return {'Authorization': f'Bearer {api_key}'}

	def _http_get_json(self, url: str, *, headers: dict[str, str], timeout_s: float = 30.0) -> Any:
		req = urllib.request.Request(url, method='GET', headers=headers)
		with urllib.request.urlopen(req, timeout=timeout_s) as resp:
			raw = resp.read().decode('utf-8', errors='replace')
		return json.loads(raw)

	def _http_get_text(self, url: str, *, headers: dict[str, str], timeout_s: float = 30.0) -> str:
		req = urllib.request.Request(url, method='GET', headers=headers)
		with urllib.request.urlopen(req, timeout=timeout_s) as resp:
			return resp.read().decode('utf-8', errors='replace')

	def _context7_resolve_library_id(self, *, library_name: str, query: str) -> Any:
		base = self._context7_base_url()
		params = urllib.parse.urlencode({'libraryName': library_name, 'query': query})
		url = f'{base}/api/v2/libs/search?{params}'
		return self._http_get_json(url, headers=self._context7_headers(), timeout_s=30.0)

	def _context7_query_docs(self, *, library_id: str, query: str, tokens: int | None) -> str:
		base = self._context7_base_url()
		p: dict[str, Any] = {'libraryId': library_id, 'query': query}
		if tokens is not None:
			p['tokens'] = int(tokens)
		params = urllib.parse.urlencode(p)
		url = f'{base}/api/v2/context?{params}'
		return self._http_get_text(url, headers=self._context7_headers(), timeout_s=60.0)

	def _init_internal_tools(self) -> None:
		self._internal_tools = [
			types.Tool(
				name='context7_resolve_library_id',
				description='Search Context7 for a library and return matching Context7-compatible library IDs.',
				inputSchema={
					'type': 'object',
					'properties': {
						'libraryName': {'type': 'string', 'description': 'Library/package name to search for.'},
						'query': {'type': 'string', 'description': 'What you need the library docs for.'},
					},
					'required': ['libraryName', 'query'],
				},
			),
			types.Tool(
				name='context7_query_docs',
				description='Fetch up-to-date documentation context from Context7 for a given libraryId.',
				inputSchema={
					'type': 'object',
					'properties': {
						'libraryId': {
							'type': 'string',
							'description': 'Context7-compatible library ID (e.g., /vercel/next.js).',
						},
						'query': {'type': 'string', 'description': 'Topic/question to focus docs on.'},
						'tokens': {
							'type': 'integer',
							'description': 'Max tokens of docs to retrieve (Context7 default is 5000).',
							'default': 5000,
						},
					},
					'required': ['libraryId', 'query'],
				},
			),
			types.Tool(
				name='docker_vm_run',
				description='Run a command inside a temporary Docker container (optionally with a repo mounted).',
				inputSchema={
					'type': 'object',
					'properties': {
						'image': {
							'type': 'string',
							'description': 'Docker image to run.',
							'default': 'alpine:3.19',
						},
						'command': {
							'type': 'string',
							'description': 'Shell command to execute inside the container (via `sh -lc`).',
						},
						'workdir': {
							'type': 'string',
							'description': 'Working directory inside container.',
							'default': '/workspace',
						},
						'repo_path': {
							'type': 'string',
							'description': 'Host path of a repo to mount at /workspace/repo.',
						},
						'repo_url': {
							'type': 'string',
							'description': 'Git URL to clone on the host and mount at /workspace/repo.',
						},
						'env': {
							'type': 'object',
							'description': 'Extra environment variables for the container.',
							'additionalProperties': {'type': 'string'},
						},
						'files': {
							'type': 'array',
							'description': 'Optional files to create under /workspace/input (base64-encoded).',
							'items': {
								'type': 'object',
								'properties': {
									'path': {'type': 'string'},
									'content_b64': {'type': 'string'},
								},
								'required': ['path', 'content_b64'],
							},
						},
						'timeout_s': {
							'type': 'integer',
							'description': 'Max seconds to allow the container run.',
							'default': 600,
						},
					},
					'required': ['command'],
				},
			),
		]

		async def _handle_context7_resolve(args: dict[str, Any]) -> list[types.Content]:
			library_name = (args.get('libraryName') or '').strip()
			query = (args.get('query') or '').strip()
			if not library_name or not query:
				return [types.TextContent(type='text', text='Error: libraryName and query are required')]
			if not self._context7_api_key() and not _env_bool('CONTEXT7_ALLOW_UNAUTHENTICATED', False):
				return [
					types.TextContent(
						type='text',
						text='Error: CONTEXT7_API_KEY is not set (set CONTEXT7_ALLOW_UNAUTHENTICATED=true to try without a key).',
					)
				]
			try:
				result = await asyncio.to_thread(self._context7_resolve_library_id, library_name=library_name, query=query)
				return [types.TextContent(type='text', text=json.dumps(result, ensure_ascii=False, indent=2))]
			except Exception as exc:
				return [types.TextContent(type='text', text=f'Error: {type(exc).__name__}: {exc}')]

		async def _handle_context7_query(args: dict[str, Any]) -> list[types.Content]:
			library_id = (args.get('libraryId') or '').strip()
			query = (args.get('query') or '').strip()
			if not library_id or not query:
				return [types.TextContent(type='text', text='Error: libraryId and query are required')]
			if not self._context7_api_key() and not _env_bool('CONTEXT7_ALLOW_UNAUTHENTICATED', False):
				return [
					types.TextContent(
						type='text',
						text='Error: CONTEXT7_API_KEY is not set (set CONTEXT7_ALLOW_UNAUTHENTICATED=true to try without a key).',
					)
				]
			tokens = args.get('tokens')
			try:
				text = await asyncio.to_thread(self._context7_query_docs, library_id=library_id, query=query, tokens=tokens)
				return [types.TextContent(type='text', text=text)]
			except Exception as exc:
				return [types.TextContent(type='text', text=f'Error: {type(exc).__name__}: {exc}')]

		async def _handle_docker_vm_run(args: dict[str, Any]) -> list[types.Content]:
			image = (args.get('image') or 'alpine:3.19').strip()
			command = (args.get('command') or '').strip()
			workdir = (args.get('workdir') or '/workspace').strip()
			repo_path = (args.get('repo_path') or '').strip()
			repo_url = (args.get('repo_url') or '').strip()
			env = args.get('env') if isinstance(args.get('env'), dict) else {}
			timeout_s_raw = args.get('timeout_s', 600)
			try:
				timeout_s = int(timeout_s_raw)
			except Exception:
				timeout_s = 600

			if not command:
				return [types.TextContent(type='text', text='Error: command is required')]
			if repo_path and repo_url:
				return [types.TextContent(type='text', text='Error: provide only one of repo_path or repo_url')]

			try:
				subprocess.run(['docker', 'version'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
			except Exception as exc:
				return [types.TextContent(type='text', text=f'Error: Docker not available: {type(exc).__name__}: {exc}')]

			try:
				with tempfile.TemporaryDirectory(prefix='mcp-plus-vm-') as tmp:
					tmp_path = Path(tmp)
					mount_repo: str | None = None

					if repo_url:
						clone_dir = tmp_path / 'repo'
						clone_dir.parent.mkdir(parents=True, exist_ok=True)
						subprocess.run(
							['git', 'clone', '--depth', '1', repo_url, str(clone_dir)],
							check=True,
							stdout=subprocess.PIPE,
							stderr=subprocess.PIPE,
							text=True,
							timeout=timeout_s,
						)
						mount_repo = str(clone_dir)
					elif repo_path:
						host_repo = Path(repo_path).expanduser().resolve()
						if not host_repo.exists() or not host_repo.is_dir():
							return [types.TextContent(type='text', text=f'Error: repo_path not found: {repo_path}')]
						mount_repo = str(host_repo)

					input_dir = tmp_path / 'input'
					input_dir.mkdir(parents=True, exist_ok=True)
					files = args.get('files') if isinstance(args.get('files'), list) else []
					for entry in files:
						if not isinstance(entry, dict):
							continue
						rel = (entry.get('path') or '').lstrip('/').strip()
						b64 = (entry.get('content_b64') or '').strip()
						if not rel or not b64:
							continue
						out_path = input_dir / rel
						out_path.parent.mkdir(parents=True, exist_ok=True)
						out_path.write_bytes(base64.b64decode(b64))

					docker_cmd: list[str] = ['docker', 'run', '--rm']
					docker_cmd += ['-w', workdir]
					docker_cmd += ['-v', f'{input_dir}:/workspace/input:rw']
					if mount_repo:
						docker_cmd += ['-v', f'{mount_repo}:/workspace/repo:rw']

					# Pass through common LLM + Context7 config by default (can be overridden).
					pass_env = {
						'OPENAI_API_BASE': os.getenv('OPENAI_API_BASE', ''),
						'OPENAI_BASE_URL': os.getenv('OPENAI_BASE_URL', ''),
						'OPENAI_API_KEY': os.getenv('OPENAI_API_KEY', ''),
						'CONTEXT7_API_KEY': os.getenv('CONTEXT7_API_KEY', ''),
					}
					for k, v in pass_env.items():
						if v:
							docker_cmd += ['-e', f'{k}={v}']
					for k, v in (env or {}).items():
						if not k:
							continue
						docker_cmd += ['-e', f'{k}={v}']

					docker_cmd += [image, 'sh', '-lc', command]

					proc = subprocess.run(
						docker_cmd,
						stdout=subprocess.PIPE,
						stderr=subprocess.PIPE,
						text=True,
						timeout=timeout_s,
					)

					result = {
						'image': image,
						'workdir': workdir,
						'mounted_repo': mount_repo,
						'command': command,
						'exit_code': proc.returncode,
						'stdout': proc.stdout,
						'stderr': proc.stderr,
					}
					return [types.TextContent(type='text', text=json.dumps(result, ensure_ascii=False, indent=2))]
			except subprocess.TimeoutExpired as exc:
				return [types.TextContent(type='text', text=f'Error: TimeoutExpired: {exc}')]
			except Exception as exc:
				return [types.TextContent(type='text', text=f'Error: {type(exc).__name__}: {exc}')]

		self._internal_handlers = {
			'context7_resolve_library_id': _handle_context7_resolve,
			'context7_query_docs': _handle_context7_query,
			'docker_vm_run': _handle_docker_vm_run,
		}

	def _init_children(self) -> None:
		enable_browser_use = _env_bool('MCP_PLUS_ENABLE_BROWSER_USE', True)
		enable_ui_describe = _env_bool('MCP_PLUS_ENABLE_UI_DESCRIBE', True)
		enable_devtools = _env_bool('MCP_PLUS_ENABLE_CHROME_DEVTOOLS', True)

		if enable_browser_use:
			self._children['browser-use'] = _ChildServer(
				name='browser-use',
				command=[sys.executable, '-m', 'browser_use.mcp'],
				cwd=str(self._repo_root),
			)
		if enable_ui_describe:
			self._children['ui-describe'] = _ChildServer(
				name='ui-describe',
				command=[sys.executable, str(self._repo_root / 'servers' / 'ui_describe_mcp_server.py')],
				cwd=str(self._repo_root),
			)
		if enable_devtools:
			self._children['chrome-devtools'] = _ChildServer(
				name='chrome-devtools',
				command=[sys.executable, str(self._repo_root / 'servers' / 'chrome_devtools_mcp_server.py')],
				cwd=str(self._repo_root),
			)

	async def _ensure_tools_loaded(self) -> None:
		async with self._tools_lock:
			if self._tools_cache is not None:
				return

			tools: list[types.Tool] = []
			routes: dict[str, _RoutedTool] = {}

			for child_name, child in self._children.items():
				await asyncio.to_thread(child.start)
				resp = await asyncio.to_thread(child.request, 'tools/list', {}, timeout_s=30.0)
				raw_tools = (resp.get('result') or {}).get('tools') or []
				for t in raw_tools:
					if not isinstance(t, dict):
						continue
					orig_name = (t.get('name') or '').strip()
					if not orig_name:
						continue
					unified_name = f'{child_name}.{orig_name}'
					routes[unified_name] = _RoutedTool(child=child_name, tool=orig_name)
					tools.append(
						types.Tool(
							name=unified_name,
							description=str(t.get('description') or ''),
							inputSchema=t.get('inputSchema') or {'type': 'object'},
						)
					)

			tools.extend(self._internal_tools)
			self._tool_routes = routes
			self._tools_cache = tools

	def _register_handlers(self) -> None:
		@self.server.list_tools()
		async def handle_list_tools() -> list[types.Tool]:
			await self._ensure_tools_loaded()
			assert self._tools_cache is not None
			return list(self._tools_cache)

		@self.server.call_tool()
		async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.Content]:
			await self._ensure_tools_loaded()
			internal = self._internal_handlers.get(name)
			if internal is not None:
				try:
					return await internal(arguments or {})
				except Exception as exc:
					logger.error('internal tool failed', exc_info=True)
					return [types.TextContent(type='text', text=f'Error: {type(exc).__name__}: {exc}')]

			route = self._tool_routes.get(name)
			if route is None:
				return [types.TextContent(type='text', text=f'Error: Unknown tool: {name}')]

			child = self._children.get(route.child)
			if child is None:
				return [types.TextContent(type='text', text=f'Error: Child server not available: {route.child}')]

			try:
				resp = await asyncio.to_thread(
					child.request,
					'tools/call',
					{'name': route.tool, 'arguments': arguments or {}},
					timeout_s=90.0,
				)
				content = (resp.get('result') or {}).get('content') or []
				return [_content_from_dict(c) for c in content]
			except Exception as exc:
				logger.error('tool proxy failed', exc_info=True)
				return [types.TextContent(type='text', text=f'Error: {type(exc).__name__}: {exc}')]

	def _install_signal_handlers(self) -> None:
		try:
			loop = asyncio.get_running_loop()
		except RuntimeError:
			return

		def _shutdown() -> None:
			for child in list(self._children.values()):
				try:
					child.close()
				except Exception:
					pass

		for sig in (signal.SIGINT, signal.SIGTERM):
			try:
				loop.add_signal_handler(sig, _shutdown)
			except Exception:
				continue

	async def run(self) -> None:
		async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
			await self.server.run(
				read_stream,
				write_stream,
				InitializationOptions(
					server_name='mcp-plus',
					server_version='0.1.0',
					capabilities=self.server.get_capabilities(
						notification_options=NotificationOptions(),
						experimental_capabilities={},
					),
				),
			)


async def main() -> None:
	server = UnifiedMCPServer()
	await server.run()


if __name__ == '__main__':
	asyncio.run(main())
