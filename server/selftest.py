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
    sent = [(u, p) for u, p in sent if u.endswith("/live-summary")]       # 안건 분류 결과는 test_agenda_* 에서 본다
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


def agenda_batches(mid, minutes=6):
    """10초마다 한 묶음, 4초마다 한 줄. 앞 절반은 배포 일정 이야기, 뒤 절반은 예산 이야기."""
    out, n, i = [], 0, 0
    for sec in range(0, minutes * 60, 10):
        n += 1
        lines = []
        while i * 4000 < (sec + 10) * 1000:
            topic = "배포 일정" if i * 4000 < minutes * 30 * 1000 else "예산 한도"
            lines.append({"seq": i + 1, "startMs": i * 4000, "endMs": i * 4000 + 3500, "text": f"{topic} 이야기 {i}번",
                          "kind": None, "lowConfidence": False, "recovered": False, "manual": False})
            i += 1
        out.append({"meetingId": mid, "batch": n, "lines": lines, "edits": []})
    return out, i


def test_agenda_flow(cap):
    """안건 목록 받기 -> 줄 분류 -> 바뀐 줄만 PUT, 고친 분류는 고정, 종료에서 final (AI-02)"""
    sent = []
    pub = Publisher("http://backend", sender=lambda url, p: (sent.append((url, p)) or (True, False, 200)))
    reg = Registry(cfg_from_env({}), publisher=pub)
    mid = "m_ag"
    reg.receive_event(mid, {"meetingId": mid, "event": "start", "atMs": 0})
    ag = dict(load("agendas-put.json"), meetingId=mid)
    assert reg.receive_agendas(mid, ag) == {"accepted": True, "result": "accepted", "version": 3}
    assert reg.receive_agendas(mid, ag) == {"accepted": True, "result": "stale"}
    assert reg.receive_agendas(mid, dict(ag, version=2)) == {"accepted": True, "result": "stale"}
    batches, total = agenda_batches(mid)
    for k, b in enumerate(batches, 1):
        if k == 10:                                         # 아직 오지 않은 60번 줄(240초)을 미리 고친다 (줄보다 고침이 먼저)
            r = reg.receive_corrections(mid, {"meetingId": mid, "corrections": [{"seq": 60, "agendaId": "a_103", "rev": 1}]})
            assert r == {"accepted": True, "result": "accepted", "applied": 1}, r
            assert reg.receive_corrections(mid, {"meetingId": mid, "corrections": [{"seq": 60, "agendaId": "a_101", "rev": 1}]})["result"] == "stale"
        reg.receive_batch(mid, b)
        reg.wait_idle(mid)
        pub.wait_idle()
    reg.receive_event(mid, {"meetingId": mid, "event": "end", "atMs": 360000})
    assert reg.wait_idle(mid) and pub.wait_idle()
    cs = [p for u, p in sent if u == f"http://backend/internal/meetings/{mid}/agenda-classification"]
    assert len(cs) >= 3, f"분류 결과를 여러 번 나눠 보내야 함: {len(cs)}번"
    vs = [p["version"] for p in cs]
    assert vs == sorted(set(vs)) and vs[0] > 10 ** 12, vs
    assert [p["final"] for p in cs] == [False] * (len(cs) - 1) + [True]
    assert all(p["schemaVersion"] == "0.1" and p["meetingId"] == mid for p in cs)
    seqs = [x["seq"] for p in cs for x in p["lines"]]
    assert 60 not in seqs, "사람이 고쳐서 고정한 줄을 결과에 담음"
    assert sorted(seqs) == [s for s in range(1, total + 1) if s != 60] and len(seqs) == len(set(seqs)), "바뀐 줄만 한 번씩 담아야 함"
    by = {x["seq"]: x["agendaId"] for p in cs for x in p["lines"]}
    assert by[1] == "a_101" and by[2] == "a_101" and by[total] == "a_102", (by[1], by[total])
    assert cs[0]["currentAgendaId"] == "a_101" and cs[-1]["currentAgendaId"] == "a_102"
    st = reg.status(mid)
    assert st["agendaClassification"]["agendas"] == 3 and st["agendaClassification"]["locked"] == 1
    assert st["agendaClassification"]["pending"] == 0 and st["agendaClassification"]["halted"] is None
    dump = json.dumps(st, ensure_ascii=False) + "\n".join(cap.rows)
    for secret in ("배포 일정 이야기", "예산 한도 이야기", "QA 담당 정하기", "예산 배분", "1차 배포 날짜"):
        assert secret not in dump, f"로그나 상태에 전사·안건 내용이 있음: {secret}"
    # 끝난 회의에는 더 받지 않는다
    assert reg.receive_agendas(mid, dict(ag, version=9))["result"] == "ignored"
    assert reg.receive_corrections(mid, {"meetingId": mid, "corrections": []})["result"] == "ignored"
    # 끈 상태(BACKEND_BASE_URL 없음)에서는 분류해도 보내지 않는다
    off = Registry(cfg_from_env({}), publisher=Publisher(None))
    off.receive_agendas("x", dict(ag, meetingId="x"))
    off.receive_batch("x", empty_batch("x", 1))
    off.wait_idle("x")
    assert off.status("x")["publisher"]["enabled"] is False


def test_agenda_rejects_and_not_ready():
    reg = Registry(cfg_from_env({}))
    ok = {"agendaId": "a", "position": 1, "title": "t", "decisionPoint": "d"}
    for fn, bad, path_id in (
            (reg.receive_agendas, {"meetingId": "m", "version": 1, "agendas": [ok, dict(ok)]}, "m"),         # agendaId 겹침
            (reg.receive_agendas, {"meetingId": "m", "version": 1, "agendas": [dict(ok, title="")]}, "m"),
            (reg.receive_agendas, {"meetingId": "m", "version": 1, "agendas": []}, "다른회의"),             # 주소와 불일치
            (reg.receive_corrections, {"meetingId": "m", "corrections": [{"seq": 1, "rev": 1}]}, "m"),        # agendaId 키 없음
            (reg.receive_corrections, {"meetingId": "m", "corrections": [{"seq": 1, "agendaId": "a", "rev": 1}]}, "다른회의")):
        try:
            fn(path_id, bad)
            raise SystemExit(f"잘못된 요청을 받아들임: {bad}")
        except AdapterError:
            pass
    assert reg.receive_agendas("m", {"meetingId": "m", "version": 1, "agendas": []})["result"] == "accepted"   # 빈 목록은 받는다
    reg.shutting_down = True
    for call in (lambda: reg.receive_agendas("m", {"meetingId": "m", "version": 2, "agendas": []}),
                 lambda: reg.receive_corrections("m", {"meetingId": "m", "corrections": []})):
        try:
            call()
            raise SystemExit("종료 중인데 받음")
        except NotReady:
            pass


def test_agenda_worker_isolated():
    """분류가 실패해도 요약은 계속되고, 요약이 실패해도 분류는 계속된다"""
    reg = Registry(cfg_from_env({}), classify_llm=lambda u: 1 / 0)
    mid = "iso"
    reg.receive_agendas(mid, dict(load("agendas-put.json"), meetingId=mid))
    batches, _ = agenda_batches(mid, minutes=3)
    for b in batches:
        reg.receive_batch(mid, b)
        reg.wait_idle(mid)
    st = reg.status(mid)
    assert st["errors"] == 0 and st["agendaClassification"]["halted"] == "연속 실패" and st["agendaClassification"]["errors"] == 3, st
    assert st["lastBatch"] == len(batches) and st["llmCalls"] >= 1, "분류가 실패해도 요약은 돌아야 함"
    # 분류 단계에서 예외가 나도(분류기 안에서 잡지 못한 오류) 요약 단계는 계속 돌고, 일꾼도 살아 있다
    reg = Registry(cfg_from_env({}))
    mid = "iso2"
    reg.receive_batch(mid, empty_batch(mid, 1))
    assert reg.wait_idle(mid)

    def boom(batch):
        raise RuntimeError("분류 실패")
    reg.meetings[mid].classifier.receive = boom
    batches, total = agenda_batches(mid, minutes=1)
    for k, b in enumerate(batches, 2):
        reg.receive_batch(mid, dict(b, batch=k))
        assert reg.wait_idle(mid)
    st = reg.status(mid)
    assert st["errors"] == len(batches) and st["lastBatch"] == len(batches) + 1 and st["queued"] == 0, st
    assert len(reg.meetings[mid].summarizer.store.snapshot()) == total, "분류가 실패해도 요약은 줄을 받아야 함"


def test_publisher_classification():
    """분류 결과는 바뀐 줄만 담으므로 버리지 않고 합친다 (옛것을 버리는 요약과 다르다)"""
    def pl(version, *rows, final=False):
        return {"schemaVersion": "0.1", "meetingId": "m", "version": version, "atMs": version, "final": final,
                "currentAgendaId": "a", "lines": [{"seq": s, "agendaId": a} for s, a in rows]}
    # 1) 경로는 종류마다 다르다
    urls = []
    pub = Publisher("http://b/", sender=lambda u, p: (urls.append(u) or (True, False, 200)))
    pub.submit("m", pl(1, (1, "a")), kind="agenda_classification")
    pub.submit("m", {"version": 1})
    assert pub.wait_idle() and urls == ["http://b/internal/meetings/m/agenda-classification",
                                        "http://b/internal/meetings/m/live-summary"], urls
    # 2) 재시도하는 사이에 더 새 결과가 생기면 둘을 합쳐서 보낸다 (같은 줄은 새 값, version 은 큰 값)
    seen = []

    def sender(u, p):
        seen.append(p)
        if len(seen) == 1:
            pub2.submit("m", pl(2, (2, "b"), (1, None)), kind="agenda_classification")
            return False, True, 503
        return True, False, 200
    pub2 = Publisher("http://b", sender=sender, sleep=lambda s: None)
    pub2.submit("m", pl(1, (1, "a")), kind="agenda_classification")
    assert pub2.wait_idle() and len(seen) == 2, seen
    assert seen[1]["lines"] == [{"seq": 1, "agendaId": None}, {"seq": 2, "agendaId": "b"}] and seen[1]["version"] == 2
    # 3) 재시도를 다 써도 실패하면 보관했다가 다음 결과에 합친다
    seen.clear()
    codes = iter([(False, True, 503)] * 4 + [(True, False, 200)])
    pub3 = Publisher("http://b", sender=lambda u, p: (seen.append(p) or next(codes)), sleep=lambda s: None)
    pub3.submit("m", pl(1, (1, "a")), kind="agenda_classification")
    assert pub3.wait_idle() and len(seen) == 4 and pub3.info()["failed"] == 1 and pub3.info()["carriedLines"] == 1
    pub3.submit("m", pl(2, (2, "b")), kind="agenda_classification")
    assert pub3.wait_idle() and seen[4]["lines"] == [{"seq": 1, "agendaId": "a"}, {"seq": 2, "agendaId": "b"}], seen[4]
    assert pub3.info()["carriedLines"] == 0
    # 4) 4xx 는 다시 보내도 소용없으니 보관하지 않는다
    seen.clear()
    codes = iter([(False, False, 400), (True, False, 200)])
    pub4 = Publisher("http://b", sender=lambda u, p: (seen.append(p) or next(codes)), sleep=lambda s: None)
    pub4.submit("m", pl(1, (1, "a")), kind="agenda_classification")
    assert pub4.wait_idle() and pub4.info()["carriedLines"] == 0
    pub4.submit("m", pl(2, (2, "b")), kind="agenda_classification")
    assert pub4.wait_idle() and seen[1]["lines"] == [{"seq": 2, "agendaId": "b"}]
    # 5) 회의 중 요약은 예전처럼 최신 하나만 남긴다
    seen.clear()
    codes = iter([(False, True, 503), (True, False, 200)])

    def sender5(u, p):
        seen.append(p["version"])
        if p["version"] == 1:
            pub5.submit("m", {"version": 2})
        return next(codes)
    pub5 = Publisher("http://b", sender=sender5, sleep=lambda s: None)
    pub5.submit("m", {"version": 1})
    assert pub5.wait_idle() and seen == [1, 2] and pub5.info()["carriedLines"] == 0


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


def test_classify_config():
    from .service import classify_cfg_from_env
    c = classify_cfg_from_env({})
    assert (c.provider, c.refresh_sec, c.max_calls) == ("echo", 30, 200)
    c = classify_cfg_from_env({"LLM_PROVIDER": "gemini", "LLM_MODEL": "m", "CLASSIFY_REFRESH_SEC": "20",
                               "MAX_CLASSIFY_CALLS_PER_MEETING": "50"})
    assert (c.provider, c.model, c.refresh_sec, c.max_calls) == ("gemini", "m", 20, 50)
    try:
        classify_cfg_from_env({"CLASSIFY_REFRESH_SEC": "abc"})
        raise SystemExit("잘못된 설정을 못 잡음")
    except ConfigError:
        pass


def test_metrics():
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from .metrics import Metrics
    reader = InMemoryMetricReader()
    m = Metrics([reader])
    m.record_token_usage("gemini", "gemini-x", "live_summary", 1200, 80)
    m.record_token_usage("gemini", "gemini-x", "live_summary", 800, 40)
    m.record_operation("gemini", "gemini-x", "live_summary", 0.25)
    m.record_operation("openai", "gpt-x", "draft", 1.5, "TimeoutError")
    token_pts, duration_pts = {}, {}
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for met in sm.metrics:
                if met.name == "gen_ai.client.token.usage":
                    assert met.unit == "{token}"
                    for dp in met.data.data_points:
                        a = dict(dp.attributes)
                        token_pts[a["gen_ai.token.type"]] = (dp.sum, dp.count, a)
                elif met.name == "gen_ai.client.operation.duration":
                    assert met.unit == "s"
                    for dp in met.data.data_points:
                        a = dict(dp.attributes)
                        duration_pts[a.get("error.type")] = (dp.sum, dp.count, a)
                else:
                    raise AssertionError(f"예상하지 못한 지표: {met.name}")
    assert token_pts["input"][:2] == (2000, 2) and token_pts["output"][:2] == (120, 2), token_pts
    a = token_pts["input"][2]
    assert a["gen_ai.provider.name"] == "gcp.gemini" and a["gen_ai.request.model"] == "gemini-x"
    assert a["ai.task"] == "live_summary" and a["gen_ai.operation.name"] == "chat"
    assert duration_pts[None][:2] == (0.25, 1), duration_pts
    assert duration_pts["TimeoutError"][:2] == (1.5, 1), duration_pts
    failed = duration_pts["TimeoutError"][2]
    assert failed["gen_ai.provider.name"] == "openai" and failed["ai.task"] == "draft"
    m.shutdown()


def test_llm_operation_hook():
    from summary import meeting_summary
    old_operation, old_usage = meeting_summary.OPERATION_HOOK, meeting_summary.USAGE_HOOK
    calls = []
    meeting_summary.OPERATION_HOOK = lambda *args: calls.append(args)
    meeting_summary.USAGE_HOOK = None
    try:
        assert meeting_summary._invoke_llm("gemini", "gemini-x", "live_summary", lambda: "ok") == "ok"

        def fail():
            raise TimeoutError("시험용 실패")

        try:
            meeting_summary._invoke_llm("openai", "gpt-x", "draft", fail)
            raise AssertionError("LLM 호출 예외를 숨김")
        except TimeoutError:
            pass
    finally:
        meeting_summary.OPERATION_HOOK, meeting_summary.USAGE_HOOK = old_operation, old_usage
    assert len(calls) == 2, calls
    assert calls[0][:3] == ("gemini", "gemini-x", "live_summary") and calls[0][3] >= 0 and calls[0][4] is None
    assert calls[1][:3] == ("openai", "gpt-x", "draft") and calls[1][3] >= 0 and calls[1][4] == "TimeoutError"


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
    main.metrics.record_operation("gemini", "gemini-x", "live_summary", 0.25, "TimeoutError")
    r = c.get("/metrics")
    assert r.status_code == 200 and "gen_ai_client_token_usage" in r.text, r.text[:300]
    assert "gen_ai_client_operation_duration" in r.text and 'ai_task="live_summary"' in r.text, r.text[:500]
    assert 'error_type="TimeoutError"' in r.text, r.text[:500]


def test_api_agenda():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        return
    from . import main
    c = TestClient(main.app)
    mid = "m_api_ag"
    ag, co = f"/internal/meetings/{mid}/agendas", f"/internal/meetings/{mid}/agenda-corrections"
    body = dict(load("agendas-put.json"), meetingId=mid)
    r = c.put(ag, json=body)
    assert r.status_code == 200 and r.json() == {"accepted": True, "result": "accepted", "version": 3}, r.text
    assert c.put(ag, json=body).json() == {"accepted": True, "result": "stale"}
    assert c.post(ag, json=body).status_code == 405                                       # 안건 목록은 PUT 만
    r = c.put(ag, json=dict(body, version=4, agendas=[body["agendas"][0], body["agendas"][0]]))
    assert r.status_code == 400 and r.json()["error"] == "INVALID_INPUT" and "agendaId" in r.json()["detail"], r.text
    assert c.put(ag.replace(mid, "other"), json=body).status_code == 400                 # 주소와 본문의 meetingId 불일치
    assert c.put(ag, content="x", headers={"content-type": "application/json"}).json()["error"] == "INVALID_INPUT"
    r = c.post(co, json=dict(load("agenda-corrections-post.json"), meetingId=mid))
    assert r.status_code == 200 and r.json() == {"accepted": True, "result": "accepted", "applied": 2}, r.text
    assert c.post(co, json=dict(load("agenda-corrections-post.json"), meetingId=mid)).json()["result"] == "stale"
    r = c.post(co, json={"meetingId": mid, "corrections": [{"seq": 1, "rev": 1}]})
    assert r.status_code == 400 and "agendaId" in r.json()["detail"], r.text
    s = c.get(f"/internal/meetings/{mid}/status").json()["agendaClassification"]
    assert s["agendaVersion"] == 3 and s["agendas"] == 3 and s["locked"] == 2, s
    main.registry.shutting_down = True
    try:
        assert c.put(ag, json=dict(body, version=9)).status_code == 503
        assert c.post(co, json={"meetingId": mid, "corrections": []}).json()["error"] == "NOT_READY"
    finally:
        main.registry.shutting_down = False


def test_agenda_summary_flow(cap):
    """안건 보드: 안건별 요약이 전체 상태로 가고, 고친 문장은 고정되며, 종료에서 마지막 상태를 보낸다 (AI-03)"""
    sent = []
    pub = Publisher("http://backend", sender=lambda url, p: (sent.append((url, p)) or (True, False, 200)), board_interval=0)
    reg = Registry(cfg_from_env({}), publisher=pub)
    mid = "m_sum"
    board_url = f"http://backend/internal/meetings/{mid}/agenda-board"
    boards = lambda: [p for u, p in sent if u == board_url]
    reg.receive_event(mid, {"meetingId": mid, "event": "start", "atMs": 0})
    ag = dict(load("agendas-put.json"), meetingId=mid)
    reg.receive_agendas(mid, ag)
    assert reg.wait_idle(mid) and pub.wait_idle()
    assert len(boards()) == 1 and all(a["summary"] == [] for a in boards()[0]["agendas"]), "목록을 받으면 빈 요약으로 한 번 보냄"
    assert [a["agendaId"] for a in boards()[0]["agendas"]] == ["a_101", "a_102", "a_103"]
    batches, total = agenda_batches(mid)
    edited = False
    for k, b in enumerate(batches, 1):
        reg.receive_batch(mid, b)
        reg.wait_idle(mid)
        pub.wait_idle()
        if k == 12 and not edited:                          # 요약이 하나 나온 뒤에 사람이 고친다
            first = boards()[-1]["agendas"][0]["summary"]
            assert first, "12번째 묶음까지 첫 안건의 요약이 나와야 함"
            r = reg.receive_summary_edits(mid, {"meetingId": mid, "edits": [
                {"agendaId": "a_101", "itemId": first[0]["id"], "action": "edit", "text": "사람이 쓴 요약 문장",
                 "evidenceSeqs": first[0]["evidenceSeqs"], "rev": 1}]})
            assert r == {"accepted": True, "result": "accepted", "applied": 1, "ignored": []}, r
            r = reg.receive_summary_edits(mid, {"meetingId": mid, "edits": [
                {"agendaId": "a_101", "itemId": first[0]["id"], "action": "edit", "text": "낡은 글", "evidenceSeqs": [1], "rev": 1},
                {"agendaId": "a_101", "itemId": "nope", "action": "confirm", "rev": 1}]})
            assert r == {"accepted": True, "result": "stale", "applied": 0,
                         "ignored": [{"itemId": first[0]["id"], "reason": "stale"}, {"itemId": "nope", "reason": "not_edited"}]}, r
            edited, edited_id = True, first[0]["id"]
            assert reg.wait_idle(mid) and pub.wait_idle()
            shown = boards()[-1]["agendas"][0]["summary"]            # 다음 묶음을 기다리지 않고 바로 반영된다
            assert any(x["id"] == edited_id and x["edited"] and x["text"] == "사람이 쓴 요약 문장" for x in shown), shown
    reg.receive_event(mid, {"meetingId": mid, "event": "end", "atMs": 360000})
    assert reg.wait_idle(mid) and pub.wait_idle()
    bs = boards()
    assert len(bs) >= 3, f"안건 보드를 여러 번 보내야 함: {len(bs)}번"
    vs = [p["version"] for p in bs]
    assert vs == sorted(set(vs)) and vs[0] > 10 ** 12, vs
    assert all("meetingId" not in p and "final" not in p and p["offTopic"] is None for p in bs), "연동 API 초안 3.2에 없는 필드"
    from agenda_status import STATUSES
    assert all(len(p["agendas"]) == 3 and all(a["status"] in STATUSES and a["opinions"] == [] for a in p["agendas"]) for p in bs)
    assert all(set(a["statusReason"]) == {"text", "evidenceSeqs"} for p in bs for a in p["agendas"] if a["statusReason"])
    last = bs[-1]
    a1, a2, a3 = last["agendas"]
    assert a3["status"] == "NOT_STARTED" and a3["statusReason"] is None, a3        # 줄이 없는 안건
    assert a1["status"] in ("DECIDED", "DECISION_CHECK", "UNDECIDED") and a1["statusReason"]["evidenceSeqs"], a1   # 끝난 뒤에는 논의 중이 없다
    assert all(a["status"] != "DISCUSSING" for a in last["agendas"]), last
    assert a1["summary"] and a2["summary"] and a3["summary"] == [], last
    assert last["currentAgendaId"] == "a_102"
    mine = [x for x in a1["summary"] if x["id"] == edited_id]
    assert mine == [{"id": edited_id, "text": "사람이 쓴 요약 문장", "evidenceSeqs": mine[0]["evidenceSeqs"], "edited": True,
                     "needsCheck": False}], a1["summary"]
    assert all(x["evidenceSeqs"] and all(1 <= s <= total for s in x["evidenceSeqs"]) for a in last["agendas"] for x in a["summary"])
    assert all(max(x["evidenceSeqs"]) <= 60 for x in a1["summary"]), "앞 절반(배포 일정) 이야기에 뒷부분 줄이 근거로 들어감"
    assert min(x["evidenceSeqs"][0] for x in a2["summary"]) >= 46, "예산 이야기 안건에 앞부분 줄이 근거로 들어감"
    ids = [x["id"] for a in last["agendas"] for x in a["summary"]]
    assert len(ids) == len(set(ids))
    assert last["basedOnSeq"] >= total - 6, last["basedOnSeq"]
    st = reg.status(mid)["agendaSummary"]
    assert st["sentences"] == len(ids) and st["edited"] == 1 and st["halted"] is None and st["errors"] == 0, st
    dump = json.dumps(reg.status(mid), ensure_ascii=False) + "\n".join(cap.rows)
    for secret in ("배포 일정 이야기", "예산 한도 이야기", "사람이 쓴 요약 문장", "낡은 글", "QA 담당 정하기", "1차 배포 날짜"):
        assert secret not in dump, f"로그나 상태에 내용이 있음: {secret}"
    # 끝난 회의에는 더 받지 않는다. 형식이 틀리면 거절
    assert reg.receive_summary_edits(mid, {"meetingId": mid, "edits": []})["result"] == "ignored"
    for bad in ({"meetingId": mid}, {"meetingId": "other", "edits": []},
                {"meetingId": mid, "edits": [{"agendaId": "a_101", "itemId": "x", "action": "edit", "rev": 1}]}):
        try:
            reg.receive_summary_edits(mid, bad)
            raise SystemExit(f"잘못된 요청을 못 잡음: {bad}")
        except AdapterError:
            pass
    # 끈 상태(BACKEND_BASE_URL 없음): 요약은 하되 보내지 않는다
    off = Registry(cfg_from_env({}), publisher=Publisher(None))
    off.receive_agendas("y", dict(ag, meetingId="y"))
    off.receive_batch("y", empty_batch("y", 1))
    off.wait_idle("y")
    assert off.status("y")["publisher"]["enabled"] is False and off.status("y")["agendaSummary"]["errors"] == 0


def test_summary_config():
    from .service import summarize_cfg_from_env
    d = summarize_cfg_from_env({})
    assert (d.provider, d.refresh_sec, d.max_calls, d.min_new_lines, d.max_sentences) == ("echo", 60, 300, 2, 3)
    c = summarize_cfg_from_env({"LLM_PROVIDER": "gemini", "LLM_MODEL": "m", "AGENDA_SUMMARY_REFRESH_SEC": "45",
                                "MAX_AGENDA_SUMMARY_CALLS_PER_MEETING": "99"})
    assert (c.provider, c.model, c.refresh_sec, c.max_calls) == ("gemini", "m", 45, 99)
    for key in ("AGENDA_SUMMARY_REFRESH_SEC", "MAX_AGENDA_SUMMARY_CALLS_PER_MEETING"):
        try:
            summarize_cfg_from_env({key: "x"})
            raise SystemExit(f"잘못된 설정을 못 잡음: {key}")
        except ConfigError:
            pass


def test_agenda_summary_end():
    """회의가 끝나면 기다리던 마지막 줄까지 요약해 마지막 상태를 보낸다"""
    sent = []
    pub = Publisher("http://backend", sender=lambda url, p: (sent.append((url, p)) or (True, False, 200)), board_interval=0)
    reg = Registry(cfg_from_env({}), publisher=pub)
    mid = "m_end"
    reg.receive_event(mid, {"meetingId": mid, "event": "start", "atMs": 0})
    reg.receive_agendas(mid, dict(load("agendas-put.json"), meetingId=mid))
    line = {"seq": 1, "startMs": 0, "endMs": 3500, "text": "배포 일정 이야기 0번", "kind": None, "lowConfidence": False,
            "recovered": False, "manual": False}
    reg.receive_batch(mid, {"meetingId": mid, "batch": 1, "lines": [line], "edits": []})
    assert reg.wait_idle(mid) and pub.wait_idle()
    boards = lambda: [p for u, p in sent if u.endswith("/agenda-board")]
    assert all(a["summary"] == [] for a in boards()[-1]["agendas"]), "줄이 1개이고 30초가 안 지났으면 아직 요약하지 않음"
    reg.receive_event(mid, {"meetingId": mid, "event": "end", "atMs": 5000})
    assert reg.wait_idle(mid) and pub.wait_idle()
    first = boards()[-1]["agendas"][0]["summary"]
    assert len(first) == 1 and first[0]["evidenceSeqs"] == [1], boards()[-1]


def test_agenda_summary_isolated():
    """요약 단계가 실패해도 분류는 계속되고, 분류가 실패해도 요약 단계는 돈다"""
    sent = []
    pub = Publisher("http://backend", sender=lambda url, p: (sent.append((url, p)) or (True, False, 200)), board_interval=0)

    def boom(user):
        raise RuntimeError("summary down")
    reg = Registry(cfg_from_env({}), publisher=pub, summarize_llm=boom)
    mid = "m_iso"
    reg.receive_event(mid, {"meetingId": mid, "event": "start", "atMs": 0})
    reg.receive_agendas(mid, dict(load("agendas-put.json"), meetingId=mid))
    batches, total = agenda_batches(mid, minutes=3)
    for b in batches:
        reg.receive_batch(mid, b)
        reg.wait_idle(mid)
    reg.receive_event(mid, {"meetingId": mid, "event": "end", "atMs": 180000})
    assert reg.wait_idle(mid) and pub.wait_idle()
    cls = [p for u, p in sent if u.endswith("/agenda-classification")]
    assert sorted(x["seq"] for p in cls for x in p["lines"]) == list(range(1, total + 1)), "요약이 막혀도 분류는 끝까지 가야 함"
    st = reg.status(mid)
    assert st["agendaSummary"]["halted"] == "연속 실패" and st["agendaSummary"]["errors"] == 3 and st["errors"] == 0, st
    assert all(a["summary"] == [] for u, p in sent if u.endswith("/agenda-board") for a in p["agendas"])
    # 분류 단계가 예외를 내도 요약 단계는 돈다 (분류는 마쳤지만 그 뒤에서 예외가 난 경우)
    reg3 = Registry(cfg_from_env({}), publisher=pub)
    m3 = reg3._get("w")
    real = m3.classifier.process

    def label_then_fail(final=False):
        real(final=final)
        raise RuntimeError("classify tail failed")
    m3.classifier.process = label_then_fail
    reg3.receive_agendas("w", dict(load("agendas-put.json"), meetingId="w"))
    for b in agenda_batches("w", minutes=2)[0]:
        reg3.receive_batch("w", b)
    assert reg3.wait_idle("w") and pub.wait_idle()
    wb = [p for u, p in sent if u.endswith("/meetings/w/agenda-board")]
    assert wb and any(a["summary"] for a in wb[-1]["agendas"]), "분류 단계 예외 때문에 요약 단계가 멈춤"
    # 요약 쪽이 예외를 내도 일꾼은 계속 돈다
    reg2 = Registry(cfg_from_env({}), publisher=pub)
    m = reg2._get("z")
    m.agenda_summarizer.process = lambda final=False: 1 / 0
    reg2.receive_agendas("z", dict(load("agendas-put.json"), meetingId="z"))
    for b in agenda_batches("z", minutes=1)[0]:
        reg2.receive_batch("z", b)
    assert reg2.wait_idle("z") and reg2.status("z")["errors"] >= 1 and reg2.status("z")["agendaClassification"]["classified"] > 0


def test_publisher_board():
    """안건 보드: 가장 큰 version 하나만 남기고, 실패하면 보관했다가 다음에 최신으로 보내고, 1초 간격을 지킨다"""
    sent, fail = [], [True]

    def sender(u, p):
        sent.append((u, p["version"]))
        return (False, True, 503) if fail[0] else (True, False, 204)
    pub = Publisher("http://backend", sender=sender, sleep=lambda s: None, board_interval=0)
    pub.submit("m", {"version": 5, "agendas": []}, kind="agenda_board")
    assert pub.wait_idle()
    assert [v for _, v in sent] == [5, 5, 5, 5] and sent[0][0] == "http://backend/internal/meetings/m/agenda-board"
    assert pub.stats["failed"] == 1 and ("m", "agenda_board") in pub.carry
    fail[0] = False
    sent.clear()
    pub.submit("m", {"version": 4, "agendas": []}, kind="agenda_board")        # 보관한 것보다 작은 version 은 이기지 못한다
    assert pub.wait_idle() and [v for _, v in sent] == [5], sent
    sent.clear()
    pub.submit("m", {"version": 9, "agendas": []}, kind="agenda_board")
    assert pub.wait_idle() and [v for _, v in sent] == [9]
    # 간격: 같은 회의의 보드는 interval 보다 가깝게 보내지 않는다 (가짜 시계)
    now, sent2 = [100.0], []
    pub2 = Publisher("http://backend", sender=lambda u, p: (sent2.append((now[0], p["version"])) or (True, False, 204)),
                     board_interval=1.0, clock=lambda: now[0])
    pub2.submit("m", {"version": 1, "agendas": []}, kind="agenda_board")
    assert pub2.wait_idle() and sent2 == [(100.0, 1)]
    pub2.submit("m", {"version": 2, "agendas": []}, kind="agenda_board")
    pub2.submit("m", {"version": 3, "agendas": []}, kind="agenda_board")
    assert not pub2.wait_idle(0.3), "간격이 지나기 전에 보냄"
    assert pub2.info()["sent"] == 1
    now[0] = 101.5
    assert pub2.wait_idle(3.0) and sent2[-1] == (101.5, 3) and len(sent2) == 2, sent2          # 기다리는 사이 쌓인 것은 최신 하나만
    pub2.submit("other", {"version": 1, "agendas": []}, kind="agenda_board")          # 다른 회의는 기다리지 않는다
    assert pub2.wait_idle(3.0) and sent2[-1] == (101.5, 1)


def test_numeric_meeting_id():
    """meetingId 가 숫자로 와도 받는다 (연동 API 초안의 공통 규칙). 주소는 문자열이므로 같은 값인지 비교하고,
    백엔드로 돌려보내는 본문에는 받은 타입 그대로 쓴다."""
    sent = []
    pub = Publisher("http://backend", sender=lambda url, p: (sent.append((url, p)) or (True, False, 200)))
    reg = Registry(cfg_from_env({}), publisher=pub)
    mid = 31
    path = str(mid)                                                          # 주소의 {meetingId}
    assert reg.receive_event(path, {"meetingId": mid, "event": "start", "atMs": 0})["result"] == "accepted"
    ag = dict(load("agendas-put.json"), meetingId=mid)
    assert reg.receive_agendas(path, ag)["result"] == "accepted"
    batches, _ = agenda_batches(mid, minutes=2)
    for b in batches:
        assert reg.receive_batch(path, b)["result"] == "accepted"
        assert reg.wait_idle(path)
    assert reg.receive_corrections(path, {"meetingId": mid, "corrections": [{"seq": 5, "agendaId": "a_102", "rev": 1}]})["result"] == "accepted"
    assert reg.receive_event(path, {"meetingId": mid, "event": "end", "atMs": 120000})["result"] == "accepted"
    assert reg.wait_idle(path) and pub.wait_idle()
    assert path in reg.meetings and len(reg.meetings) == 1, "숫자와 문자열이 다른 회의로 갈라짐"
    kinds = {u.rsplit("/", 1)[1] for u, _ in sent}
    assert kinds == {"live-summary", "agenda-classification", "agenda-board"}, kinds
    assert all("/internal/meetings/31/" in u for u, _ in sent), [u for u, _ in sent]
    assert all(p["meetingId"] == 31 and isinstance(p["meetingId"], int) for u, p in sent if not u.endswith("/agenda-board")), \
        "받은 타입 그대로 돌려주지 않음"
    assert reg.status(path)["meetingId"] in (31, "31")
    # 같은 회의를 문자열로 보내도 같은 회의로 본다. 주소와 값이 다르면 거절한다
    assert reg.receive_event(path, {"meetingId": "31", "event": "start", "atMs": 0})["result"] == "duplicate"
    for bad_path, bad_body in (("32", {"meetingId": 31, "event": "pause", "atMs": 5}),
                               ("31", {"meetingId": 32, "event": "pause", "atMs": 5}),
                               ("31", {"meetingId": True, "event": "pause", "atMs": 5}),
                               ("31", {"meetingId": 31.0, "event": "pause", "atMs": 5})):
        try:
            reg.receive_event(bad_path, bad_body)
            raise SystemExit(f"잘못된 meetingId 를 받아 버림: {bad_path} {bad_body}")
        except AdapterError:
            pass
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        return
    from . import main
    c = TestClient(main.app)
    r = c.post("/internal/meetings/77/events", json={"meetingId": 77, "event": "start", "atMs": 0})
    assert r.status_code == 200 and r.json()["accepted"] is True, r.text
    assert c.post("/internal/meetings/77/events", json={"meetingId": 78, "event": "pause", "atMs": 1}).status_code == 400
    assert c.put("/internal/meetings/77/agendas", json={"meetingId": 77, "version": 1, "agendas": []}).status_code == 200


def test_api_summary_edits():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        return
    from . import main
    c = TestClient(main.app)
    mid = "31"
    url = f"/internal/meetings/{mid}/agenda-summary-edits"
    body = load("agenda-summary-edits-post.json")
    r = c.post(url, json=body)
    assert r.status_code == 200 and r.json() == {"accepted": True, "result": "accepted", "applied": 2, "ignored": []}, r.text   # edit 뒤에 confirm
    r = c.post(url, json=body)
    assert r.json() == {"accepted": True, "result": "stale", "applied": 0,
                        "ignored": [{"itemId": "k3f9a1-2", "reason": "stale"}, {"itemId": "k3f9a1-2", "reason": "stale"}]}, r.text
    assert c.put(url, json=body).status_code == 405                                      # 고친 요약은 POST 만
    r = c.post(url, json={"meetingId": 31, "edits": [{"agendaId": 101, "itemId": "x", "action": "edit", "rev": 1}]})
    assert r.status_code == 400 and r.json()["error"] == "INVALID_INPUT" and "text" in r.json()["detail"], r.text
    assert c.post(url.replace(mid, "32"), json=body).status_code == 400                  # 주소와 본문의 meetingId 불일치
    s = c.get(f"/internal/meetings/{mid}/status").json()["agendaSummary"]
    assert s["edited"] == 1 and s["halted"] is None, s
    main.registry.shutting_down = True
    try:
        assert c.post(url, json=body).status_code == 503
        assert c.post(url, json=body).json()["error"] == "NOT_READY"
    finally:
        main.registry.shutting_down = False


ITEM_LINES = ["배포는 목요일로 하죠", "배포 테스트 일정 때문에 어려워요", "배포 목요일은 부담이에요", "배포 금요일은 어때요",
              "배포 철수가 점검표 수요일까지 공유할게요", "배포 체크리스트는 제가 할게요", "배포 영수가 사용량 확인하겠습니다"]


def item_batches(mid, extra=0):
    """의견 4종류와 할 일 3개가 나오는 7줄을 3개 묶음으로 보내고, 뒤에 2줄짜리 묶음을 extra 개 더 붙인다. 모두 첫 안건(배포) 이야기."""
    def line(seq, text):
        return {"seq": seq, "startMs": (seq - 1) * 4000, "endMs": (seq - 1) * 4000 + 3500, "text": text,
                "kind": None, "lowConfidence": False, "recovered": False, "manual": False}
    rows = [line(i + 1, t) for i, t in enumerate(ITEM_LINES)]
    groups = [rows[0:3], rows[3:5], rows[5:7]]
    seq = len(rows)
    for k in range(extra):
        groups.append([line(seq + 1, f"배포 계속 이야기 {k}번"), line(seq + 2, f"배포 계속 이야기 {k}번 더")])
        seq += 2
    return [{"meetingId": mid, "batch": n, "lines": g, "edits": []} for n, g in enumerate(groups, 1)]


def test_item_cfg():
    from .service import opinion_cfg_from_env, todo_cfg_from_env
    d = opinion_cfg_from_env({})
    assert (d.provider, d.refresh_sec, d.min_lines, d.max_calls, d.min_new_lines, d.max_items) == ("echo", 60, 4, 300, 2, 8)
    c = opinion_cfg_from_env({"LLM_PROVIDER": "gemini", "LLM_MODEL": "m", "AGENDA_OPINION_REFRESH_SEC": "45",
                              "AGENDA_OPINION_MIN_LINES": "6", "MAX_AGENDA_OPINION_CALLS_PER_MEETING": "99"})
    assert (c.provider, c.model, c.refresh_sec, c.min_lines, c.max_calls) == ("gemini", "m", 45, 6, 99)
    for key in ("AGENDA_OPINION_REFRESH_SEC", "AGENDA_OPINION_MIN_LINES", "MAX_AGENDA_OPINION_CALLS_PER_MEETING"):
        try:
            opinion_cfg_from_env({key: "x"})
            raise SystemExit(f"잘못된 설정을 못 잡음: {key}")
        except ConfigError:
            pass
    d = todo_cfg_from_env({})
    assert (d.provider, d.refresh_sec, d.min_lines, d.max_calls, d.min_new_lines, d.max_items) == ("echo", 60, 1, 300, 2, 20)
    c = todo_cfg_from_env({"LLM_PROVIDER": "gemini", "LLM_MODEL": "m", "AGENDA_TODO_REFRESH_SEC": "45",
                           "MAX_AGENDA_TODO_CALLS_PER_MEETING": "99"})
    assert (c.provider, c.model, c.refresh_sec, c.max_calls) == ("gemini", "m", 45, 99)
    for key in ("AGENDA_TODO_REFRESH_SEC", "MAX_AGENDA_TODO_CALLS_PER_MEETING"):
        try:
            todo_cfg_from_env({key: "x"})
            raise SystemExit(f"잘못된 설정을 못 잡음: {key}")
        except ConfigError:
            pass


def test_agenda_items_flow(cap):
    """안건 보드에 의견과 할 일이 담기고, 고친 항목은 고정되며, 참석자 명단이 바뀌면 모델 없이 이름을 다시 맞춘다 (AI-05)"""
    from agenda_classifier import ClassifyCfg
    sent = []
    pub = Publisher("http://backend", sender=lambda url, p: (sent.append((url, p)) or (True, False, 200)), board_interval=0)
    reg = Registry(cfg_from_env({}), publisher=pub, classify_cfg=ClassifyCfg(refresh_sec=0))   # 묶음마다 바로 분류한다
    mid, path = 31, "31"
    boards = lambda: [p for u, p in sent if u.endswith("/agenda-board")]
    reg.receive_event(path, {"meetingId": mid, "event": "start", "atMs": 0})
    ag = dict(load("agendas-with-attendees-put.json"), meetingId=mid)
    assert reg.receive_agendas(path, ag) == {"accepted": True, "result": "accepted", "version": 4}
    assert reg.wait_idle(path) and pub.wait_idle()
    assert all(a["opinions"] == [] and a["todos"] == [] for a in boards()[0]["agendas"]), "목록만 받았을 때는 빈 배열"
    assert reg.status(path)["agendaTodos"]["roster"] == 2
    for b in item_batches(mid):
        reg.receive_batch(path, b)
        assert reg.wait_idle(path) and pub.wait_idle()
    last = boards()[-1]
    a1 = last["agendas"][0]
    assert a1["agendaId"] == 101 and all(a["todos"] == [] and a["opinions"] == [] for a in last["agendas"][1:])
    assert [x["kind"] for x in a1["opinions"]] == ["OPINION", "EVIDENCE", "CONCERN", "QUESTION"], a1["opinions"]
    assert [x["evidenceSeqs"] for x in a1["opinions"]] == [[1], [2], [3], [4]]
    todos = a1["todos"]
    assert [(t["assigneeName"], t["assigneeStatus"], t["assigneeUserId"], t["dueText"], t["unclear"]) for t in todos] == [
        ("철수", "MATCHED", 7, "수요일까지", False), (None, "UNASSIGNED", None, None, False),
        ("영수", "NOT_IN_ROSTER", None, None, False)], todos
    assert [t["evidenceSeqs"] for t in todos] == [[5], [6], [7]]
    assert all(set(t) == {"id", "task", "assigneeName", "assigneeStatus", "assigneeUserId", "dueText", "evidenceSeqs", "edited",
                          "needsCheck", "unclear"} for t in todos)
    assert last["basedOnSeq"] >= 7
    ids = [x["id"] for x in a1["opinions"]] + [t["id"] for t in todos]
    assert len(ids) == len(set(ids))
    json.dumps(last)

    # 사람이 고친 의견과 할 일: 바로 반영되고 고친 항목이 맨 앞에 온다
    op_id, td_id = a1["opinions"][1]["id"], todos[1]["id"]
    r = reg.receive_item_edits(path, {"meetingId": mid, "edits": [
        {"agendaId": 101, "target": "opinion", "itemId": op_id, "action": "edit", "text": "사람이 쓴 근거", "kind": "EVIDENCE",
         "evidenceSeqs": [2], "rev": 1},
        {"agendaId": 101, "target": "todo", "itemId": td_id, "action": "edit", "task": "체크리스트 작성", "assigneeName": "영희",
         "dueText": "금요일까지", "evidenceSeqs": [6], "rev": 1}]})
    assert r == {"accepted": True, "result": "accepted", "applied": 2, "ignored": []}, r
    r = reg.receive_item_edits(path, {"meetingId": mid, "edits": [
        {"agendaId": 101, "target": "todo", "itemId": td_id, "action": "edit", "task": "낡은 글", "evidenceSeqs": [6], "rev": 1},
        {"agendaId": 101, "target": "opinion", "itemId": "nope", "action": "confirm", "rev": 1}]})
    assert r == {"accepted": True, "result": "stale", "applied": 0,
                 "ignored": [{"itemId": "nope", "reason": "not_edited"}, {"itemId": td_id, "reason": "stale"}]}, r
    assert reg.wait_idle(path) and pub.wait_idle()
    a1 = boards()[-1]["agendas"][0]
    assert a1["opinions"][0] == {"id": op_id, "text": "사람이 쓴 근거", "kind": "EVIDENCE", "evidenceSeqs": [2], "edited": True,
                                 "needsCheck": False}, a1["opinions"]
    t0 = a1["todos"][0]
    assert t0["id"] == td_id and t0["edited"] is True and (t0["assigneeName"], t0["assigneeStatus"], t0["assigneeUserId"], t0["dueText"]) == \
        ("영희", "MATCHED", 8, "금요일까지"), t0
    assert any(t["assigneeName"] == "철수" and t["assigneeStatus"] == "MATCHED" and not t["edited"] for t in a1["todos"])

    # 참석자 명단이 바뀌면 모델을 부르지 않고 이름을 다시 맞춘다. 같은 version 이면 명단도 무시한다
    calls = reg.status(path)["agendaTodos"]["calls"]
    assert reg.receive_agendas(path, dict(ag, version=4, attendees=[]))["result"] == "stale"
    assert reg.status(path)["agendaTodos"]["roster"] == 2, "낡은 version 의 명단을 받아 버림"
    roster2 = ag["attendees"] + [{"userId": 9, "name": "박영수"}]
    assert reg.receive_agendas(path, dict(ag, version=5, attendees=roster2))["result"] == "accepted"
    assert reg.wait_idle(path) and pub.wait_idle()
    a1 = boards()[-1]["agendas"][0]
    assert any(t["assigneeName"] == "영수" and t["assigneeStatus"] == "MATCHED" and t["assigneeUserId"] == 9 for t in a1["todos"]), a1["todos"]
    assert reg.status(path)["agendaTodos"]["calls"] == calls, "명단이 바뀌었다고 모델을 부름"
    assert reg.receive_agendas(path, dict(ag, version=6, attendees=None))["result"] == "accepted"      # 필드가 비어 있으면 이전 명단을 둔다
    assert reg.status(path)["agendaTodos"]["roster"] == 3
    assert reg.receive_agendas(path, dict(ag, version=7, attendees=[]))["result"] == "accepted"        # 빈 배열은 명단이 비었다는 뜻
    assert reg.wait_idle(path) and pub.wait_idle()
    a1 = boards()[-1]["agendas"][0]
    assert reg.status(path)["agendaTodos"]["roster"] == 0
    assert all(t["assigneeStatus"] in (None, "UNASSIGNED") for t in a1["todos"]), "명단이 비면 이름을 맞추지 않는다"
    reg.receive_agendas(path, dict(ag, version=8))
    # 끝나면 마지막 상태를 보내고, 끝난 회의에는 더 받지 않는다
    reg.receive_event(path, {"meetingId": mid, "event": "end", "atMs": 60000})
    assert reg.wait_idle(path) and pub.wait_idle()
    assert boards()[-1]["agendas"][0]["opinions"][0]["edited"] is True
    assert reg.receive_item_edits(path, {"meetingId": mid, "edits": []})["result"] == "ignored"
    st = reg.status(path)
    assert st["agendaOpinions"]["items"] >= 5 and st["agendaOpinions"]["edited"] == 1 and st["agendaOpinions"]["halted"] is None, st
    assert st["agendaTodos"]["edited"] == 1 and st["agendaTodos"]["halted"] is None and st["errors"] == 0, st
    dump = json.dumps(st, ensure_ascii=False) + "\n".join(cap.rows)
    for secret in ("철수", "영희", "영수", "점검표", "체크리스트", "사용량", "사람이 쓴 근거", "목요일로 하죠", "배포 일정"):
        assert secret not in dump, f"로그나 상태에 내용이 있음: {secret}"
    # 잘못된 요청은 거절
    reg2 = Registry(cfg_from_env({}))
    bad_edit = {"agendaId": 101, "target": "opinion", "itemId": "x", "action": "edit", "text": "글", "kind": "OPINION", "evidenceSeqs": [1], "rev": 1}
    for bad in ({"meetingId": "m"}, {"meetingId": "other", "edits": []},
                {"meetingId": "m", "edits": [dict(bad_edit, target="summary")]},
                {"meetingId": "m", "edits": [dict(bad_edit, kind="MAYBE")]},
                {"meetingId": "m", "edits": [{k: v for k, v in bad_edit.items() if k != "text"}]}):
        try:
            reg2.receive_item_edits("m", bad)
            raise SystemExit(f"잘못된 요청을 못 잡음: {bad}")
        except AdapterError:
            pass
    try:
        reg2.receive_agendas("m", {"meetingId": "m", "version": 1, "agendas": [], "attendees": [{"userId": 1}]})
        raise SystemExit("잘못된 참석자 명단을 못 잡음")
    except AdapterError:
        pass


def test_agenda_items_isolated():
    """의견이 멈춰도 할 일과 보드 전송은 계속되고, 할 일이 멈춰도 의견은 계속된다. 한 단계가 예외를 내도 다른 단계는 돈다"""
    from agenda_classifier import ClassifyCfg
    fast = ClassifyCfg(refresh_sec=0)                       # 묶음마다 바로 분류한다

    def run(mid, **kw):
        sent = []
        pub = Publisher("http://backend", sender=lambda url, p: (sent.append((url, p)) or (True, False, 200)), board_interval=0)
        reg = Registry(cfg_from_env({}), publisher=pub, classify_cfg=fast, **kw)
        reg.receive_event(mid, {"meetingId": mid, "event": "start", "atMs": 0})
        reg.receive_agendas(mid, dict(load("agendas-with-attendees-put.json"), meetingId=mid))
        for b in item_batches(mid, extra=3):
            reg.receive_batch(mid, b)
            reg.wait_idle(mid)
        reg.receive_event(mid, {"meetingId": mid, "event": "end", "atMs": 90000})
        assert reg.wait_idle(mid) and pub.wait_idle()
        return reg, [p for u, p in sent if u.endswith("/agenda-board")]

    def boom(user):
        raise RuntimeError("model down")
    reg, bs = run("m_i1", opinion_llm=boom)
    st = reg.status("m_i1")
    assert st["agendaOpinions"]["halted"] == "연속 실패" and st["agendaOpinions"]["calls"] == 3 and st["agendaOpinions"]["errors"] == 3, st
    assert st["agendaTodos"]["halted"] is None and st["errors"] == 0, st
    a1 = bs[-1]["agendas"][0]
    assert a1["opinions"] == [] and len(a1["todos"]) == 3, a1
    reg, bs = run("m_i2", todo_llm=boom)
    st = reg.status("m_i2")
    assert st["agendaTodos"]["halted"] == "연속 실패" and st["agendaOpinions"]["halted"] is None and st["errors"] == 0, st
    a1 = bs[-1]["agendas"][0]
    assert a1["todos"] == [] and [x["kind"] for x in a1["opinions"]] == ["OPINION", "EVIDENCE", "CONCERN", "QUESTION"], a1
    assert reg.status("m_i2")["agendaClassification"]["classified"] > 0, "분류와 요약은 계속된다"
    # 단계 하나가 예외를 내도 일꾼은 계속 돌고, 다른 단계와 보드 전송은 계속된다
    sent = []
    pub = Publisher("http://backend", sender=lambda url, p: (sent.append((url, p)) or (True, False, 200)), board_interval=0)
    reg = Registry(cfg_from_env({}), publisher=pub, classify_cfg=fast)
    m = reg._get("m_i3")
    m.opinions.process = lambda final=False: 1 / 0
    reg.receive_agendas("m_i3", dict(load("agendas-with-attendees-put.json"), meetingId="m_i3"))
    for b in item_batches("m_i3"):
        reg.receive_batch("m_i3", b)
        reg.wait_idle("m_i3")
    assert reg.wait_idle("m_i3") and pub.wait_idle()
    assert reg.status("m_i3")["errors"] >= 1
    last = [p for u, p in sent if u.endswith("/agenda-board")][-1]["agendas"][0]
    assert len(last["todos"]) == 3 and last["opinions"] == [], "의견 단계 예외 때문에 할 일이나 보드 전송이 멈춤"
    # 보드에 담을 값을 읽다가 예외가 나도 보드는 전송한다 (그 칸만 빈 배열)
    m.opinions.board_items = lambda: 1 / 0
    n = len([1 for u, p in sent if u.endswith("/agenda-board")])
    reg.receive_batch("m_i3", {"meetingId": "m_i3", "batch": 4, "lines": [
        {"seq": 50, "startMs": 400000, "endMs": 403500, "text": "배포 지민이 내일까지 확인하겠습니다", "kind": None, "lowConfidence": False,
         "recovered": False, "manual": False},
        {"seq": 51, "startMs": 404000, "endMs": 407500, "text": "배포 계속 이야기", "kind": None, "lowConfidence": False,
         "recovered": False, "manual": False}], "edits": []})
    assert reg.wait_idle("m_i3") and pub.wait_idle()
    after = [p for u, p in sent if u.endswith("/agenda-board")]
    assert len(after) > n and len(after[-1]["agendas"][0]["todos"]) == 4 and after[-1]["agendas"][0]["opinions"] == []


def test_api_item_edits():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        return
    from . import main
    c = TestClient(main.app)
    mid = "41"
    url = f"/internal/meetings/{mid}/agenda-item-edits"
    body = dict(load("agenda-item-edits-post.json"), meetingId=41)
    r = c.post(url, json=body)
    assert r.status_code == 200 and r.json() == {"accepted": True, "result": "accepted", "applied": 3, "ignored": []}, r.text
    r = c.post(url, json=body)
    assert r.json() == {"accepted": True, "result": "stale", "applied": 0,
                        "ignored": [{"itemId": "k3f9a1-8", "reason": "stale"}, {"itemId": "k3f9a1-8", "reason": "stale"},
                                    {"itemId": "k3f9a1-11", "reason": "stale"}]}, r.text
    assert c.put(url, json=body).status_code == 405                                      # 고친 항목은 POST 만
    r = c.post(url, json={"meetingId": 41, "edits": [{"agendaId": 101, "target": "opinion", "itemId": "x", "action": "edit", "rev": 1}]})
    assert r.status_code == 400 and r.json()["error"] == "INVALID_INPUT" and "evidenceSeqs" in r.json()["detail"], r.text
    assert c.post(url.replace(mid, "42"), json=body).status_code == 400                  # 주소와 본문의 meetingId 불일치
    s = c.get(f"/internal/meetings/{mid}/status").json()
    assert s["agendaOpinions"]["edited"] == 1 and s["agendaTodos"]["edited"] == 1 and s["agendaOpinions"]["halted"] is None, s
    # 안건 목록에 참석자 명단을 실어 보낸다
    agendas = dict(load("agendas-with-attendees-put.json"), meetingId=41)
    r = c.put(f"/internal/meetings/{mid}/agendas", json=agendas)
    assert r.status_code == 200 and r.json() == {"accepted": True, "result": "accepted", "version": 4}, r.text
    assert c.get(f"/internal/meetings/{mid}/status").json()["agendaTodos"]["roster"] == 2
    r = c.put(f"/internal/meetings/{mid}/agendas", json=dict(agendas, version=5, attendees=[{"userId": 1, "name": ""}]))
    assert r.status_code == 400 and r.json()["error"] == "INVALID_INPUT" and "name" in r.json()["detail"], r.text
    main.registry.shutting_down = True
    try:
        assert c.post(url, json=body).status_code == 503
        assert c.post(url, json=body).json()["error"] == "NOT_READY"
    finally:
        main.registry.shutting_down = False


def main():
    cap = Capture()
    logging.getLogger("ai-service").addHandler(cap)
    logging.getLogger("ai-service").setLevel(logging.DEBUG)
    logging.getLogger("ai-service.publisher").setLevel(logging.DEBUG)
    test_config()
    test_flow(cap)
    test_live_publish(cap)
    test_agenda_flow(cap)
    test_agenda_rejects_and_not_ready()
    test_agenda_worker_isolated()
    test_summary_config()
    test_agenda_summary_flow(cap)
    test_agenda_summary_end()
    test_agenda_summary_isolated()
    test_publisher_board()
    test_classify_config()
    test_publisher_classification()
    test_publisher_retry()
    test_publisher_http()
    test_not_ready()
    test_rejects_and_warnings()
    test_failure_does_not_stop_worker()
    test_metrics()
    test_llm_operation_hook()
    test_api()
    test_api_agenda()
    test_api_summary_edits()
    test_item_cfg()
    test_agenda_items_flow(cap)
    test_agenda_items_isolated()
    test_api_item_edits()
    test_numeric_meeting_id()
    print("수신 서버 자체 시험 통과 (설정, 묶음 번호 409, 요약 전송·재시도, 503, 잘못된 요청, 처리 실패, 토큰 지표, 로그에 전사 내용 없음, HTTP, "
          "안건 분류: 안건 목록·고친 분류 받기, 바뀐 줄만 전송, 고정, 종료 final, 실패 격리, 합쳐 보내기, 숫자 meetingId, "
          "안건 요약: 안건 보드 전송, 고친 요약 받기, 실패 격리, 1초 간격, "
          "의견·할 일: 참석자 명단 받기, 고친 항목 받기, 이름 다시 맞추기, 실패 격리)")


if __name__ == "__main__":
    main()
