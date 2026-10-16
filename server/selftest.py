#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""수신 서버 자체 시험 (저장소 루트에서: python3 -m server.selftest). 모델 호출 없음, 비용 0."""
import json
import logging
import os

from .service import Registry, ConfigError, AdapterError, cfg_from_env, HERE

EX = os.path.join(HERE, "..", "examples", "api")
load = lambda n: json.load(open(os.path.join(EX, n), encoding="utf-8"))
TEXTS = ["배포는 다음 주 목요일", "벡터디비로 가죠", "일정부터 정할까요"]       # 로그에 나오면 안 되는 전사 내용


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        self.rows.append(record.getMessage())


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
    reg = Registry(cfg_from_env({}))
    mid = "m_1234"
    assert reg.receive_event(mid, load("meeting-event-start.json"))["result"] == "accepted"
    r = reg.receive_batch(mid, load("lines-batch-basic.json"))              # batch 7 : 1~6이 빠져 있음
    assert r["result"] == "accepted" and r["gap"] == {"from": 1, "to": 6}, r
    assert reg.receive_batch(mid, load("lines-batch-basic.json"))["result"] == "duplicate"
    assert reg.receive_batch(mid, load("lines-batch-with-edits.json"))["result"] == "accepted"
    assert reg.receive_event(mid, load("meeting-event-pause.json"))["result"] == "accepted"
    assert reg.receive_event(mid, load("meeting-event-pause.json"))["result"] == "duplicate"
    assert reg.receive_event(mid, load("meeting-event-resume.json"))["result"] == "accepted"
    assert reg.receive_event(mid, load("meeting-event-end.json"))["result"] == "accepted"
    assert reg.wait_idle(mid), "대기열이 비지 않음"
    s = reg.status(mid)
    assert s["status"] == "ended" and s["lastBatch"] == 8 and s["errors"] == 0 and s["queued"] == 0, s
    assert s["received"] == {"batches": 2, "lines": 5, "edits": 2}, s
    assert s["gaps"] == [{"from": 1, "to": 6}] and s["warnings"] == 0
    m = reg.meetings[mid]
    assert "end" in m.summarizer.snapshots, "종료 시점 요약이 만들어지지 않음"
    assert m.summarizer.export_summary()["schema_version"] == "0.1"
    # 종료 뒤에 온 묶음은 무시
    late = dict(load("lines-batch-basic.json"), batch=9)
    assert reg.receive_batch(mid, late)["result"] == "ignored"
    # 상태와 로그에 전사 내용이 없어야 함
    blob = json.dumps(s, ensure_ascii=False) + "\n".join(cap.rows)
    assert not any(t in blob for t in TEXTS), "상태나 로그에 전사 내용이 새어 나감"


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


def test_failure_does_not_stop_worker(cap):
    def boom(user):
        raise RuntimeError("모델 실패")
    reg = Registry(cfg_from_env({}), llm=boom)
    mid = "f"
    b = load("lines-batch-basic.json")
    b["meetingId"] = mid
    reg.receive_batch(mid, b)
    b2 = dict(b, batch=8)
    reg.receive_batch(mid, b2)
    assert reg.wait_idle(mid)
    assert reg.status(mid)["queued"] == 0         # 일꾼이 살아서 대기열을 비웠다


def test_api(cap):
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  (fastapi 가 없어 HTTP 시험은 건너뜀: pip install -r requirements.txt)")
        return
    from . import main
    c = TestClient(main.app)
    assert c.get("/health").json() == {"status": "ok"}
    mid = "m_1234"
    ev, bt = f"/internal/meetings/{mid}/events", f"/internal/meetings/{mid}/batches"
    assert c.post(ev, json=load("meeting-event-start.json")).status_code == 202
    r = c.post(bt, json=load("lines-batch-basic.json"))
    assert r.status_code == 202 and r.json()["result"] == "accepted", r.text
    assert c.post(bt, json=load("lines-batch-basic.json")).json()["result"] == "duplicate"
    r = c.post(bt, json={"meetingId": mid, "batch": "x", "lines": []})
    assert r.status_code == 422 and "batch" in r.json()["detail"], r.text
    assert c.post(bt.replace(mid, "other"), json=load("lines-batch-basic.json")).status_code == 422
    assert c.post(bt, content="not json", headers={"content-type": "application/json"}).status_code == 422
    assert c.get(f"/internal/meetings/{mid}/status").json()["lastBatch"] == 7
    assert c.get("/internal/meetings/none/status").status_code == 404


def main():
    cap = Capture()
    logging.getLogger("ai-service").addHandler(cap)
    logging.getLogger("ai-service").setLevel(logging.DEBUG)
    test_config()
    test_flow(cap)
    test_rejects_and_warnings()
    test_failure_does_not_stop_worker(cap)
    test_api(cap)
    print("수신 서버 자체 시험 통과 (설정, 흐름, 중복·건너뜀, 종료 뒤 묶음, 잘못된 요청, 처리 실패, 로그에 전사 내용 없음, HTTP)")


if __name__ == "__main__":
    main()
