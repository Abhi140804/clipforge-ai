"""Test harness: real ClipForge server (uvicorn in a thread) + a mock server that plays Anthropic, Google OAuth/YouTube and a CDN."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


DATA = tempfile.mkdtemp(prefix="clipforge-test-")
APP_PORT, MOCK_PORT = free_port(), free_port()
os.environ.update({
    "CLIPFORGE_DATA": DATA, "CLIPFORGE_PORT": str(APP_PORT), "CLIPFORGE_BASE_URL": f"http://127.0.0.1:{APP_PORT}",
    "CLIPFORGE_ALLOW_PRIVATE_URLS": "1", "CLIPFORGE_WORKERS": "1", "CLIPFORGE_ALLOW_SIGNUP": "0",
    "GOOGLE_AUTH_URL": f"http://127.0.0.1:{MOCK_PORT}/google/auth", "GOOGLE_TOKEN_URL": f"http://127.0.0.1:{MOCK_PORT}/google/token",
    "YT_API_BASE": f"http://127.0.0.1:{MOCK_PORT}/youtube/v3", "YT_UPLOAD_BASE": f"http://127.0.0.1:{MOCK_PORT}/upload/youtube/v3",
    "ANTHROPIC_API_KEY": "", "TRANSCRIBE_API_KEY": "",
})

import mock_server  # noqa: E402  (after env is set)


class Client:
    """requests session that adds the CSRF header and keeps the cookie."""

    def __init__(self, base):
        self.base, self.s = base, requests.Session()

    def req(self, method, path, **kw):
        h = {"X-CF-CSRF": "1", **kw.pop("headers", {})}
        return self.s.request(method, self.base + path, headers=h, timeout=60, allow_redirects=kw.pop("allow_redirects", True), **kw)

    def get(self, p, **kw): return self.req("GET", p, **kw)
    def post(self, p, **kw): return self.req("POST", p, **kw)
    def put(self, p, **kw): return self.req("PUT", p, **kw)
    def patch(self, p, **kw): return self.req("PATCH", p, **kw)
    def delete(self, p, **kw): return self.req("DELETE", p, **kw)


def run_uvicorn(app, port):
    import uvicorn
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return server
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


@pytest.fixture(scope="session")
def sample(tmp_path_factory):
    from make_sample import build
    d = tmp_path_factory.mktemp("sample")
    build(d, 75.0)
    return d


@pytest.fixture(scope="session")
def mock(sample):
    mock_server.VIDEO = sample / "sample.mp4"
    mock_server.TRUTH = sample / "truth.json"
    run_uvicorn(mock_server.app, MOCK_PORT)
    return mock_server.STATE


@pytest.fixture(scope="session")
def server(mock):
    from backend.app import app
    run_uvicorn(app, APP_PORT)
    from backend import assets
    assert assets.wait_ready(120), "built-in assets never became ready"
    return f"http://127.0.0.1:{APP_PORT}"


@pytest.fixture(scope="session")
def client(server):
    c = Client(server)
    r = c.post("/api/auth/signup", json={"email": "owner@example.com", "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    return c


def wait_for(fn, timeout=300, every=1.0, what="condition"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what}; last={last}")


def wait_project(client, pid, statuses=("completed", "failed", "needs_upload", "cancelled"), timeout=400):
    def f():
        p = client.get(f"/api/projects/{pid}").json()
        return p if p["status"] in statuses else None
    return wait_for(f, timeout, 1.5, f"project {pid} to reach {statuses}")
