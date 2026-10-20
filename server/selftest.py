#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""수신 서버 자체 시험 (저장소 루트에서: python3 -m server.selftest). 모델 호출 없음, 비용 0."""
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import Registry, ConfigError, AdapterError, BatchOutOfOrder, NotReady, cfg_from_env, HERE
from .publisher import Publisher

EX = os.path.join(HERE, "..", "examples", "api")
load = lambda n: json.load(open(os.path.join(EX, n), encoding="utf-8"))
TEXTS = ["배포는 다음 주 목요일", "벡터디비로 가죠", "일정부터 정할까요", "테스트 문장"]   # 로그에 나오면 안 되는 전사 내용


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        self.rows.append(record.getMessage())


def empty_batch(mid, n):
    return {"meetingId": mid, "batch": n, "lines": [], "edits": []}


def feed_empty(reg, mid, upto):
    for n in range(1, upto + 1):
        assert reg.receive_batch(mid, empty_batch(mid, n))["result"] == "accepted"


def synthetic_batches(mid, minutes=10):
    """10초마다 한 묶음, 4초마다 한 줄. 요약이 여러 번 바뀌는 회의."""
    out, n, i = [], 0, 0
    for sec in range(0, minutes * 60, 10):
        n += 1
        lines = []
        while i * 4000 < (sec + 10) * 1000:
            lines.append({"seq": i + 1, "startMs": i * 4000, "endMs": i * 4000 + 3500, "text": f"테스트 문장 {i}번 이야기",
                          "kind": None, "lowConfidence": False, "recovered": False, "manual": False})
            i += 1
        out.append({"meetingId": mid, "batch": n, "lines": lines, "edits": []})
    return out


def test_config():
    assert cfg_from_env({}).provider == "echo"
    c = cfg_from_env({"LLM_PROVIDER": "gemini", "LLM_MODEL": "m", "WINDOW_SEC": "90"})
    assert (c.provider, c.model, c.window_sec) == ("gemini", "m", 90)
    for bad in ({"LLM_PROVIDER": "nope"}, {"WINDOW_SEC": "abc"}):
        try:
            cfg_from_env(bad)
            raise SystemExit(f"잘못된 설정을 못 잡음: {bad}")
        except ConfigError:
            pass


def test_flow(cap):
    """예시 JSON 6개 흐름 + 묶음 번호 규칙 (A1, A2)"""
    sent = []
    pub = Publisher("http://backend", sender=lambda url, p: (sent.append(p) or (True, False, 200)))
    reg = Registry(cfg_from_env({}), publisher=pub)
    mid = "m_1234"
    assert reg.receive_event(mid, load("meeting-event-start.json"))["result"] == "accepted"
    # 재시작 직후처럼 모르는 회의의 7번이 오면 1번부터 다시 보내 달라고 한다
    try:
        reg.receive_batch(mid, load("lines-batch-basic.json"))
        raise SystemExit("모르는 회의의 7번 묶음을 받아 버림")
    except BatchOutOfOrder as e:
        assert e.expected == 1
    feed_empty(reg, mid, 6)
    r = reg.receive_batch(mid, load("lines-batch-basic.json"))
    assert r["accepted"] is True and r["result"] == "accepted" and r["batch"] == 7, r
    assert reg.receive_batch(mid, load("lines-batch-basic.json"))["result"] == "duplicate"
    try:                                                                    # 8번을 건너뛰고 9번
        reg.receive_batch(mid, dict(load("lines-batch-with-edits.json"), batch=9))
        raise SystemExit("건너뛴 번호를 받아 버림")
    except BatchOutOfOrder as e:
        assert e.expected == 8
    assert reg.receive_batch(mid, load("lines-batch-with-edits.json"))["result"] == "accepted"
    for name in ("pause", "resume", "end"):
        assert reg.receive_event(mid, load(f"meeting-event-{name}.json"))["result"] == "accepted"
    assert reg.receive_event(mid, load("meeting-event-pause.json"))["result"] == "duplicate"
    assert reg.wait_idle(mid) and pub.wait_idle()
    s = reg.status(mid)
    assert s["status"] == "ended" and s["lastBatch"] == 8 and s["errors"] == 0 and s["queued"] == 0, s
    assert s["received"] == {"batches": 8, "lines": 5, "edits": 2} and s["outOfOrder"] == 2 and s["warnings"] == 0, s
    assert "end" in reg.meetings[mid].summarizer.snapshots
    assert sent and sent[-1]["final"] is True and sent[-1]["meetingId"] == mid, "종료 요약이 전송되지 않음"
    late = dict(load("lines-batch-basic.json"), batch=9)                    # 종료 뒤 묶음은 무시
    assert reg.receive_batch(mid, late) == {"accepted": False, "result": "ignored", "reason": "ended", "batch": 9}
    blob = json.dumps(s, ensure_ascii=False) + "\n".join(cap.rows)
    assert not any(t in blob for t in TEXTS), "상태나 로그에 전사 내용이 새어 나감"


def test_live_publish(cap):
    """회의 중 요약이 바뀔 때마다 보내고, version은 늘어나며, 종료에서 final (A7)"""
    sent = []
    pub = Publisher("http://backend", sender=lambda url, p: (sent.append((url, p)) or (True, False, 200)))
    reg = Registry(cfg_from_env({}), publisher=pub)
    mid = "m_live"
    reg.receive_event(mid, {"meetingId": mid, "event": "start", "atMs": 0})
    for b in synthetic_batches(mid):
        reg.receive_batch(mid, b)
        reg.wait_idle(mid)
        pub.wait_idle()
    reg.receive_event(mid, {"meetingId": mid, "event": "end", "atMs": 600000})
    assert reg.wait_idle(mid) and pub.wait_idle()
    ps = [p for _, p in sent]
    assert len(ps) >= 3, f"요약이 바뀔 때마다 보내야 함: {len(ps)}번"
    assert all(u == "http://backend/internal/meetings/m_live/live-summary" for u, _ in sent)
    vs = [p["version"] for p in ps]
    assert vs == sorted(set(vs)) and vs[0] > 10 ** 12, vs                   # 늘어나기만 하고, 시각(밀리초) 이상
    assert [p["final"] for p in ps] == [False] * (len(ps) - 1) + [True]
    assert ps[0]["summary"] and set(ps[0]["summary"][0]) == {"text", "topic", "evidence"}, ps[0]
    assert ps[-1]["schemaVersion"] == "0.1" and ps[-1]["atMs"] >= 590000
    assert not any(t in "\n".join(cap.rows) for t in TEXTS), "로그에 전사 내용이 있음"
    # 끈 상태(BACKEND_BASE_URL 없음)에서는 보내지 않고 상태에도 그렇게 나온다
    off = Registry(cfg_from_env({}), publisher=Publisher(None))
    off.receive_batch("x", empty_batch("x", 1))
    off.wait_idle("x")
    assert off.status("x")["publisher"]["enabled"] is False


def test_publisher_retry():
    # 1) 5xx 는 3번까지 다시 보낸다 (1초·2초·4초 간격), 성공하면 멈춘다
    calls, sleeps = [], []
    results = iter([(False, True, 503), (False, True, 503), (True, False, 200)])
    pub = Publisher("http://b", sender=lambda u, p: (calls.append(p["version"]) or next(results)), sleep=sleeps.append)
    pub.submit("m", {"version": 1})
    assert pub.wait_idle()
    assert calls == [1, 1, 1] and sleeps == [1.0, 2.0] and pub.info()["sent"] == 1 and pub.info()["retries"] == 2
    # 2) 계속 실패하면 재시도 3회 뒤 포기
    calls.clear(); sleeps.clear()
    pub = Publisher("http://b", sender=lambda u, p: (calls.append(1) or (False, True, 503)), sleep=sleeps.append)
    pub.submit("m", {"version": 1})
    assert pub.wait_idle()
    assert len(calls) == 4 and sleeps == [1.0, 2.0, 4.0] and pub.info()["failed"] == 1
    # 3) 4xx(400 등)는 다시 보내도 소용없으니 바로 포기
    calls.clear()
    pub = Publisher("http://b", sender=lambda u, p: (calls.append(1) or (False, False, 400)), sleep=sleeps.append)
    pub.submit("m", {"version": 1})
    assert pub.wait_idle() and len(calls) == 1 and pub.info()["failed"] == 1
    # 4) 재시도하는 사이에 더 새 요약이 생기면 옛것은 버리고 새것을 보낸다
    seen = []

    def sender(u, p):
        seen.append(p["version"])
        if p["version"] == 1:
            pub4.submit("m", {"version": 2})
            return False, True, 503
        return True, False, 200
    pub4 = Publisher("http://b", sender=sender, sleep=lambda s: None)
    pub4.submit("m", {"version": 1})
    assert pub4.wait_idle() and seen == [1, 2] and pub4.info()["sent"] == 1, seen


def test_publisher_http():
    """진짜 HTTP로 PUT: 경로, 본문(한글), 503 뒤 재시도"""
    got, codes = [], [503, 200]

    class H(BaseHTTPRequestHandler):
        def do_PUT(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((self.path, self.headers["Content-Type"], json.loads(body.decode("utf-8"))))
            self.send_response(codes.pop(0) if codes else 200)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        pub = Publisher(f"http://127.0.0.1:{srv.server_address[1]}", backoffs=(0.01, 0.01, 0.01))
        pub.submit("m_9", {"version": 5, "summary": [{"text": "한글 요약"}]})
        assert pub.wait_idle()
    finally:
        srv.shutdown()
    assert len(got) == 2 and got[0][0] == "/internal/meetings/m_9/live-summary", got
    assert got[1][1] == "application/json" and got[1][2]["summary"][0]["text"] == "한글 요약"
    assert pub.info()["sent"] == 1 and pub.info()["retries"] == 1


def test_not_ready():
    reg = Registry(cfg_from_env({}))
    reg.shutting_down = True
    for call in (lambda: reg.receive_batch("m", empty_batch("m", 1)),
                 lambda: reg.receive_event("m", {"meetingId": "m", "event": "start", "atMs": 0})):
        try:
            call()
            raise SystemExit("종료 중인데 받음")
        except NotReady as e:
            assert "종료" in str(e)
    g = Registry(cfg_from_env({"LLM_PROVIDER": "gemini"}), env={})            # 키가 없으면 준비 안 됨
    ok, why = g.ready()
    assert not ok and "GEMINI_API_KEY" in why
    try:
        g.receive_batch("m", empty_batch("m", 1))
        raise SystemExit("키가 없는데 받음")
    except NotReady:
        pass
    assert Registry(cfg_from_env({"LLM_PROVIDER": "gemini"}), env={"GEMINI_API_KEY": "x"}).ready()[0]
    assert Registry(cfg_from_env({"LLM_PROVIDER": "echo"}), env={}).ready()[0]


def test_rejects_and_warnings():
    reg = Registry(cfg_from_env({}))
    for fn, bad in ((reg.receive_batch, {"meetingId": "m", "batch": "1", "lines": []}),
                    (reg.receive_event, {"meetingId": "m", "event": "stop", "atMs": 0}),
                    (reg.receive_batch, load("lines-batch-basic.json"))):          # 주소와 meetingId 불일치
        try:
            fn("m" if bad.get("meetingId") == "m" else "다른회의", bad)
            raise SystemExit(f"잘못된 요청을 받아들임: {bad}")
        except AdapterError:
            pass
    assert reg.status("없는회의") is None
    r = reg.receive_event("w", {"meetingId": "w", "event": "resume", "atMs": 5})   # 멈춘 적 없는데 resume
    assert r["result"] == "accepted" and "warning" in r and reg.status("w")["warnings"] == 1


def test_failure_does_not_stop_worker():
    reg = Registry(cfg_from_env({}))
    mid = "f"
    reg.receive_batch(mid, empty_batch(mid, 1))
    assert reg.wait_idle(mid)

    def boom(batch):
        raise RuntimeError("처리 실패")
    reg.meetings[mid].summarizer.receive = boom
    reg.receive_batch(mid, empty_batch(mid, 2))
    assert reg.wait_idle(mid) and reg.status(mid)["errors"] == 1
    reg.receive_batch(mid, empty_batch(mid, 3))                                  # 일꾼이 살아 있다
    assert reg.wait_idle(mid) and reg.status(mid)["queued"] == 0 and reg.status(mid)["lastBatch"] == 3


def test_metrics():
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from .metrics import Metrics
    reader = InMemoryMetricReader()
    m = Metrics([reader])
    m.record_token_usage("gemini", "gemini-x", "live_summary", 1200, 80)
    m.record_token_usage("gemini", "gemini-x", "live_summary", 800, 40)
    pts = {}
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for met in sm.metrics:
                assert met.name == "gen_ai.client.token.usage" and met.unit == "{token}"
                for dp in met.data.data_points:
                    a = dict(dp.attributes)
                    pts[a["gen_ai.token.type"]] = (dp.sum, dp.count, a)
    assert pts["input"][:2] == (2000, 2) and pts["output"][:2] == (120, 2), pts
    a = pts["input"][2]
    assert a["gen_ai.provider.name"] == "gcp.gemini" and a["gen_ai.request.model"] == "gemini-x"
    assert a["ai.task"] == "live_summary" and a["gen_ai.operation.name"] == "chat"
    m.shutdown()


def test_api():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  (fastapi 가 없어 HTTP 시험은 건너뜀: pip install -r requirements.txt)")
        return
    from . import main
    c = TestClient(main.app)
    assert c.get("/health").json() == {"status": "ok"}
    mid = "m_api"
    ev, bt = f"/internal/meetings/{mid}/events", f"/internal/meetings/{mid}/batches"
    assert c.post(ev, json={"meetingId": mid, "event": "start", "atMs": 0}).status_code == 200
    r = c.post(bt, json=dict(load("lines-batch-basic.json"), meetingId=mid))    # 1번이 아니라 7번이 먼저 옴
    assert r.status_code == 409 and r.json() == {"expectedBatch": 1}, r.text
    r = c.post(bt, json=empty_batch(mid, 1))
    assert r.status_code == 200 and r.json()["accepted"] is True and r.json()["batch"] == 1, r.text
    assert c.post(bt, json=empty_batch(mid, 1)).json()["result"] == "duplicate"
    r = c.post(bt, json={"meetingId": mid, "batch": "x", "lines": []})
    assert r.status_code == 400 and r.json()["error"] == "INVALID_INPUT" and "batch" in r.json()["detail"], r.text
    assert c.post(bt.replace(mid, "other"), json=empty_batch(mid, 2)).status_code == 400
    assert c.post(bt, content="not json", headers={"content-type": "application/json"}).json()["error"] == "INVALID_INPUT"
    assert c.get(f"/internal/meetings/{mid}/status").json()["lastBatch"] == 1
    assert c.get("/internal/meetings/none/status").status_code == 404
    main.registry.shutting_down = True                                           # 준비 안 됨 -> 503
    try:
        r = c.post(bt, json=empty_batch(mid, 2))
        assert r.status_code == 503 and r.json()["error"] == "NOT_READY", r.text
        assert c.get("/health").status_code == 200
    finally:
        main.registry.shutting_down = False
    main.metrics.record_token_usage("gemini", "gemini-x", "live_summary", 10, 2)
    r = c.get("/metrics")
    assert r.status_code == 200 and "gen_ai_client_token_usage" in r.text and 'ai_task="live_summary"' in r.text, r.text[:300]


def main():
    cap = Capture()
    logging.getLogger("ai-service").addHandler(cap)
    logging.getLogger("ai-service").setLevel(logging.DEBUG)
    logging.getLogger("ai-service.publisher").setLevel(logging.DEBUG)
    test_config()
    test_flow(cap)
    test_live_publish(cap)
    test_publisher_retry()
    test_publisher_http()
    test_not_ready()
    test_rejects_and_warnings()
    test_failure_does_not_stop_worker()
    test_metrics()
    test_api()
    print("수신 서버 자체 시험 통과 (설정, 묶음 번호 409, 요약 전송·재시도, 503, 잘못된 요청, 처리 실패, 토큰 지표, 로그에 전사 내용 없음, HTTP)")


if __name__ == "__main__":
    main()
