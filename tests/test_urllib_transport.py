import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mx_teams_feed.adapters.urllib_transport import UrllibTransport
from mx_teams_feed.errors import TransientError


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def _reply(self, status, body=b"", headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/slow":
            threading.Event().wait(2)
        if self.path == "/missing":
            return self._reply(404, b'{"error":"nope"}')
        echo = {"path": self.path, "auth": self.headers.get("Authorization")}
        self._reply(200, json.dumps(echo).encode(), {"Content-Type": "application/json"})

    def do_PATCH(self):
        length = int(self.headers.get("Content-Length", 0))
        received = json.loads(self.rfile.read(length))
        self._reply(200, json.dumps({"received": received}).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self._reply(201, self.rfile.read(length), {"X-Echo": self.headers.get("Content-Type")})


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_get_returns_status_headers_and_body(server):
    response = UrllibTransport().send("GET", f"{server}/x", headers={"Authorization": "Bearer t"})
    assert response.status == 200
    assert response.json() == {"path": "/x", "auth": "Bearer t"}
    assert response.header("content-type") == "application/json"


def test_error_statuses_are_returned_not_raised(server):
    response = UrllibTransport().send("GET", f"{server}/missing")
    assert response.status == 404
    assert response.json() == {"error": "nope"}


def test_json_bodies_are_encoded(server):
    response = UrllibTransport().send("PATCH", f"{server}/m", json={"subject": "s"})
    assert response.json() == {"received": {"subject": "s"}}


def test_raw_content_is_sent_as_is(server):
    response = UrllibTransport().send(
        "POST", f"{server}/u", content=b"abc", headers={"Content-Type": "text/plain"}
    )
    assert (response.status, response.body) == (201, b"abc")
    assert response.header("X-Echo") == "text/plain"


def test_timeouts_are_transient(server):
    with pytest.raises(TransientError):
        UrllibTransport().send("GET", f"{server}/slow", timeout=0.2)


def test_connection_failures_are_transient():
    with pytest.raises(TransientError):
        UrllibTransport().send("GET", "http://127.0.0.1:9/", timeout=1)
