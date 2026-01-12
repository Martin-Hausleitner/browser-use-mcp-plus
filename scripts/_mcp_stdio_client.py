from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class MCPProcess:
	name: str
	proc: subprocess.Popen[bytes]
	stdout_queue: "queue.Queue[dict[str, Any]]"
	stderr_lines: list[str]


def _reader_thread(stream: Any, out_q: "queue.Queue[dict[str, Any]]", stderr_lines: list[str]) -> None:
	while True:
		line = stream.readline()
		if not line:
			return
		try:
			text = line.decode("utf-8", errors="replace").strip()
		except Exception:
			continue
		if not text:
			continue
		try:
			msg = json.loads(text)
		except Exception:
			stderr_lines.append(text)
			continue
		out_q.put(msg)


def _stderr_thread(stream: Any, stderr_lines: list[str]) -> None:
	while True:
		line = stream.readline()
		if not line:
			return
		try:
			text = line.decode("utf-8", errors="replace").rstrip("\n")
		except Exception:
			continue
		stderr_lines.append(text)
		if len(stderr_lines) > 400:
			del stderr_lines[:200]


class MCPStdioClient:
	def __init__(
		self,
		*,
		name: str,
		command: list[str],
		env: dict[str, str] | None = None,
		cwd: str | None = None,
	) -> None:
		self._name = name
		self._command = command
		self._env = env or {}
		self._cwd = cwd
		self._id = 0
		self._proc: MCPProcess | None = None

	def start(self) -> None:
		if self._proc is not None:
			return
		env = os.environ.copy()
		env.update(self._env)

		proc = subprocess.Popen(
			self._command,
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			env=env,
			cwd=self._cwd,
		)
		assert proc.stdin and proc.stdout and proc.stderr

		out_q: "queue.Queue[dict[str, Any]]" = queue.Queue()
		stderr_lines: list[str] = []

		threading.Thread(
			target=_reader_thread,
			args=(proc.stdout, out_q, stderr_lines),
			name=f"{self._name}-stdout",
			daemon=True,
		).start()
		threading.Thread(
			target=_stderr_thread,
			args=(proc.stderr, stderr_lines),
			name=f"{self._name}-stderr",
			daemon=True,
		).start()

		self._proc = MCPProcess(name=self._name, proc=proc, stdout_queue=out_q, stderr_lines=stderr_lines)

	def close(self) -> None:
		if self._proc is None:
			return
		p = self._proc.proc
		try:
			p.terminate()
		except Exception:
			pass
		try:
			p.wait(timeout=3)
		except Exception:
			try:
				p.kill()
			except Exception:
				pass
			try:
				p.wait(timeout=3)
			except Exception:
				pass
		self._proc = None

	def _send(self, msg: dict[str, Any]) -> None:
		if self._proc is None:
			raise RuntimeError("Client not started")
		p = self._proc.proc
		assert p.stdin
		data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
		p.stdin.write(data)
		p.stdin.flush()

	def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
		msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
		if params is not None:
			msg["params"] = params
		self._send(msg)

	def request(self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float = 20.0) -> dict[str, Any]:
		self._id += 1
		req_id = self._id
		msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
		if params is not None:
			msg["params"] = params
		self._send(msg)
		return self._wait_response(req_id, timeout_s=timeout_s)

	def _wait_response(self, req_id: int, *, timeout_s: float) -> dict[str, Any]:
		if self._proc is None:
			raise RuntimeError("Client not started")
		deadline = time.time() + timeout_s
		while time.time() < deadline:
			try:
				msg = self._proc.stdout_queue.get(timeout=0.2)
			except queue.Empty:
				p = self._proc.proc
				if p.poll() is not None:
					raise RuntimeError(
						f"{self._name} exited with code {p.returncode}. stderr tail:\n"
						+ "\n".join(self._proc.stderr_lines[-50:])
					)
				continue

			if msg.get("id") == req_id:
				return msg
		raise TimeoutError(
			f"Timed out waiting for {self._name} response id={req_id}. stderr tail:\n"
			+ "\n".join((self._proc.stderr_lines if self._proc else [])[-50:])
		)

	def initialize(self, *, protocol_versions: Iterable[str] = ("2024-11-05", "2024-10-07")) -> dict[str, Any]:
		last_err: Exception | None = None
		for v in protocol_versions:
			try:
				resp = self.request(
					"initialize",
					{
						"protocolVersion": v,
						"clientInfo": {"name": "browser-use-mcp-plus", "version": "0.1.0"},
						"capabilities": {},
					},
					timeout_s=20.0,
				)
				if "error" in resp:
					raise RuntimeError(resp["error"])
				self.notify("notifications/initialized", {})
				return resp
			except Exception as e:  # noqa: BLE001
				last_err = e
				continue
		raise RuntimeError(f"Failed to initialize {self._name}") from last_err

