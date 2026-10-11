import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


class FakeSnapchat:
    """Serves memories the way Snapchat's download endpoints do."""

    def __init__(self):
        self.routes = {}
        self.hits = 0
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _key(self, method):
                url = urlparse(self.path)
                mid = parse_qs(url.query).get("mid")
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    mid = parse_qs(self.rfile.read(length).decode()).get("mid")
                    return f"POST {url.path}?mid={mid[0]}" if mid else None
                return f"{url.path}?mid={mid[0]}" if mid else url.path

            def do_GET(self):
                fake.hits += 1
                route = fake.routes.get(self._key("GET"))
                if route is None:
                    if self.path.startswith("/dmd/old"):
                        return self._reply(405, b"method not allowed")
                    return self._reply(404, b"not found")
                self._reply(*route)

            def do_POST(self):
                fake.hits += 1
                route = fake.routes.get(self._key("POST"))
                self._reply(*(route or (404, b"not found")))

        return Handler

    def close(self):
        self.server.shutdown()


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    """A synthetic export + server + phone, built once for all tests."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    from synth import build

    root = tmp_path_factory.mktemp("world")
    server = FakeSnapchat()
    export, phone, routes, expect = build(root, server.url)
    server.routes.update(routes)
    yield {"root": root, "export": export, "phone": phone, "server": server, "expect": expect}
    server.close()
