from __future__ import annotations

import contextlib
import functools
import http.server
import threading
import time
from collections.abc import Iterator
from pathlib import Path


class QuietHandler(http.server.SimpleHTTPRequestHandler):
	def log_message(self, format: str, *args) -> None:  # noqa: A002
		return


@contextlib.contextmanager
def serve_static_dir(root: Path) -> Iterator[tuple[str, str]]:
	handler = functools.partial(QuietHandler, directory=str(root))
	with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as httpd:
		port = httpd.server_address[1]
		url = f"http://127.0.0.1:{port}/"
		url_contains = f"127.0.0.1:{port}"

		thread = threading.Thread(target=httpd.serve_forever, name="fixture-httpd", daemon=True)
		thread.start()

		# Give the server a moment to come up reliably on slower systems.
		time.sleep(0.05)

		try:
			yield url, url_contains
		finally:
			httpd.shutdown()
			thread.join(timeout=2)

