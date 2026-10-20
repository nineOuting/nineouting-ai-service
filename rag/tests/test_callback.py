"""콜백 시험 (DB 없이 돈다)."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rag.callback import CallbackNotAllowed, CallbackSender, allowed_hosts_from_env, check_callback_url

BODY = {"docId": 1, "docVersion": "v1", "status": "ready", "errorCode": None, "chunkCount": 3}


class FakeBackend:
    def __init__(self, *results):
        self.calls, self.results = [], list(results)

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        return self.results.pop(0) if self.results else (True, False, 200)


def test_허용_목록_읽기():
    assert allowed_hosts_from_env({}) is None
    assert allowed_hosts_from_env({"RAG_CALLBACK_ALLOWED_HOSTS": " "}) is None
    assert allowed_hosts_from_env({"RAG_CALLBACK_ALLOWED_HOSTS": "a.svc, B.svc:8084"}) == {"a.svc", "b.svc:8084"}


@pytest.mark.parametrize("url, allowed", [
    ("http://minutes:8084/cb", None),
    ("https://anything.example/cb", None),
    ("http://minutes:8084/cb", {"minutes"}),
    ("http://minutes:8084/cb", {"minutes:8084"}),
    ("http://MINUTES:8084/cb", {"minutes"}),
])
def test_허용되는_주소(url, allowed):
    check_callback_url(url, allowed)


@pytest.mark.parametrize("url, allowed", [
    (None, None),
    ("", None),
    ("ftp://minutes/cb", None),
    ("http:///cb", None),
    ("http://user:pw@minutes/cb", None),
    ("http://169.254.169.254/latest/meta-data", {"minutes"}),
    ("http://minutes:9999/cb", {"minutes:8084"}),
])
def test_거부되는_주소(url, allowed):
    with pytest.raises(CallbackNotAllowed):
        check_callback_url(url, allowed)


def test_거부된_주소로는_보내지_않음():
    backend = FakeBackend()
    assert not CallbackSender(allowed={"minutes"}, sender=backend).send("http://other/cb", BODY, "req-1")
    assert backend.calls == []


def test_성공하면_한_번_보내고_추적_헤더를_붙임():
    backend = FakeBackend()
    assert CallbackSender(sender=backend).send("http://minutes/cb", BODY, "req-1")
    (url, body, headers), = backend.calls
    assert (url, body) == ("http://minutes/cb", BODY)
    assert headers["X-Request-Id"] == "req-1" and headers["Content-Type"] == "application/json"


def test_추적_ID가_없으면_헤더도_없음():
    backend = FakeBackend()
    CallbackSender(sender=backend).send("http://minutes/cb", BODY, None)
    assert "X-Request-Id" not in backend.calls[0][2]


def test_5xx_와_연결_오류는_재시도():
    backend = FakeBackend((False, True, 503), (False, True, None), (True, False, 200))
    sleeps = []
    assert CallbackSender(sender=backend, sleep=sleeps.append).send("http://minutes/cb", BODY)
    assert len(backend.calls) == 3 and sleeps == [1.0, 2.0]


def test_재시도를_다_쓰면_실패():
    backend = FakeBackend(*[(False, True, 500)] * 4)
    sleeps = []
    assert not CallbackSender(sender=backend, sleep=sleeps.append).send("http://minutes/cb", BODY)
    assert len(backend.calls) == 4 and sleeps == [1.0, 2.0, 4.0]


def test_4xx_는_바로_포기():
    backend = FakeBackend((False, False, 400))
    assert not CallbackSender(sender=backend, sleep=lambda s: None).send("http://minutes/cb", BODY)
    assert len(backend.calls) == 1


def test_리다이렉트를_따라가지_않음():
    """실제 HTTP 로 확인한다. 302 를 받으면 Location 으로 가지 않고 실패로 끝나야 한다."""
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/cb"
        assert not CallbackSender(timeout=2, sleep=lambda s: None).send(url, BODY)
        assert hits == ["/cb"]                       # 한 번만 왔고, 다른 곳으로 따라가지 않음
    finally:
        server.shutdown()
        server.server_close()
