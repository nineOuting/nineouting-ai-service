#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
입력 어댑터: 백엔드가 보내는 JSON(camelCase)을 요약 코드의 내부 형식(snake_case)으로 바꾼다.

형식 정의: docs/api/input-contract.md, 예시: examples/api/

변환 규칙
  lines   seq -> id, startMs -> start_ms, endMs -> end_ms, lowConfidence -> low_confidence
          recovered true -> type "recovered", 아니면 "final". manual이면 type "manual"
          text_marked = textMarked가 오면 그 값, 없으면 text (결정 B: 단어 단위 표시는 쓰지 않음)
          arrival_ms = endMs (recovered는 +2초). 백엔드가 도착 시각을 주지 않아 추정한 값
  edits   seq -> id. text나 deleted가 없는 edit(speakerLabel만 바뀐 경우 등)은 무시
  event   meetingId -> meeting_id, atMs -> at_ms

표준 라이브러리만 쓴다. 외부 호출 없음.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

RECOVERED_DELAY_MS = 2000                     # meeting_summary.py와 같은 값 (recovered 줄은 약 2초 뒤 도착)
EVENTS = ("start", "pause", "resume", "end")


class AdapterError(ValueError):
    """백엔드 입력이 형식과 다를 때."""


def _need(d, key, types, where):
    if key not in d:
        raise AdapterError(f"{where}: '{key}' 가 없음")
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, types):
        raise AdapterError(f"{where}: '{key}' 의 타입이 맞지 않음 ({type(v).__name__})")
    return v


def adapt_line(ln):
    where = f"line seq={ln.get('seq', '?')}"
    seq = _need(ln, "seq", int, where)
    start_ms = _need(ln, "startMs", int, where)
    end_ms = _need(ln, "endMs", int, where)
    text = _need(ln, "text", str, where)
    if end_ms < start_ms:
        raise AdapterError(f"{where}: endMs가 startMs보다 작음")
    recovered = bool(ln.get("recovered", False))
    manual = bool(ln.get("manual", False))
    text_marked = ln.get("textMarked") or text
    return {
        "id": seq,
        "arrival_ms": end_ms + (RECOVERED_DELAY_MS if recovered else 0),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "type": "manual" if manual else ("recovered" if recovered else "final"),
        "kind": ln.get("kind"),
        "low_confidence": bool(ln.get("lowConfidence", False)),
        "text": text,
        "text_marked": text_marked,
        "manual": manual,
    }


def adapt_edit(e):
    """쓸 수 있는 edit이면 내부 형식으로, 아니면 None (speakerLabel만 바뀐 edit 등)."""
    where = f"edit seq={e.get('seq', '?')}"
    seq = _need(e, "seq", int, where)
    rev = _need(e, "rev", int, where)
    if e.get("deleted"):
        return {"id": seq, "rev": rev, "deleted": True}
    if not isinstance(e.get("text"), str):
        return None
    return {"id": seq, "rev": rev, "text": e["text"], "text_marked": e.get("textMarked") or e["text"]}


def adapt_batch(msg):
    """백엔드 lines/edits 묶음 -> TranscriptStore.apply_batch 가 받는 dict"""
    meeting_id = _need(msg, "meetingId", str, "batch")
    batch_no = _need(msg, "batch", int, "batch")
    lines = [adapt_line(x) for x in msg.get("lines") or []]
    edits = [a for a in (adapt_edit(x) for x in msg.get("edits") or []) if a is not None]
    return {"meeting_id": meeting_id, "seq": batch_no, "lines": lines, "edits": edits}


def adapt_event(msg):
    """회의 이벤트(start / pause / resume / end)"""
    meeting_id = _need(msg, "meetingId", str, "event")
    event = _need(msg, "event", str, "event")
    if event not in EVENTS:
        raise AdapterError(f"event: 알 수 없는 값 '{event}' (start, pause, resume, end 중 하나)")
    at_ms = _need(msg, "atMs", int, "event")
    return {"meeting_id": meeting_id, "event": event, "at_ms": at_ms, "wall": msg.get("wall")}


# --------------------------------------------------------------------------------------
def selftest():
    from meeting_summary import TranscriptStore, build_input

    ex = os.path.join(HERE, "..", "examples", "api")
    load = lambda n: json.load(open(os.path.join(ex, n), encoding="utf-8"))

    # 1) 회의 이벤트 4종
    got = [adapt_event(load(f"meeting-event-{n}.json")) for n in EVENTS]
    assert [g["event"] for g in got] == list(EVENTS)
    assert got[0]["at_ms"] == 0 and got[3]["at_ms"] == 1200000 and got[0]["meeting_id"] == "m_1234"
    assert got[1]["wall"].startswith("2026-10-15T14:10")
    try:
        adapt_event({"meetingId": "m", "event": "stop", "atMs": 0})
        raise SystemExit("알 수 없는 event를 못 잡음")
    except AdapterError:
        pass

    # 2) 기본 묶음: 필드 변환, recovered 줄은 시간 순서 자리에 끼워 넣기
    b = adapt_batch(load("lines-batch-basic.json"))
    assert b["seq"] == 7 and len(b["lines"]) == 4 and b["edits"] == []
    l45 = b["lines"][0]
    assert l45["id"] == 45 and l45["type"] == "recovered" and l45["arrival_ms"] == 125000 + RECOVERED_DELAY_MS
    assert all(l["text_marked"] == l["text"] for l in b["lines"]), "textMarked가 없으면 text로 대체해야 함"
    st = TranscriptStore(b["meeting_id"])
    st.apply_batch(b)
    assert [l["id"] for l in st.snapshot()] == [45, 42, 43, 44]
    # 맞장구(43)와 낮은 신뢰도 줄(44)은 요약 입력에서 빠지고, 나머지 둘만 번호가 붙는다
    kept, dropped, text = build_input(st.snapshot())
    assert [l["id"] for l in kept] == [45, 42] and [d[1] for d in dropped] == ["kind", "low_confidence"], dropped
    assert text.splitlines()[0].startswith("[1] 02:03 그럼 일정부터")

    # 3) edits: 수정은 반영, 삭제는 제거, manual 줄은 제자리에 끼워 넣기
    st2 = TranscriptStore("m_1234")
    st2.apply_batch({"lines": [adapt_line({"seq": 38, "startMs": 100000, "endMs": 101000, "text": "삭제될 줄"}),
                               adapt_line({"seq": 40, "startMs": 128000, "endMs": 128800, "text": "벡터 디비로 가죠", "lowConfidence": True}),
                               adapt_line({"seq": 43, "startMs": 129000, "endMs": 130400, "text": "네", "kind": "backchannel"})],
                     "edits": []})
    e = adapt_batch(load("lines-batch-with-edits.json"))
    assert e["lines"][0]["type"] == "manual" and e["lines"][0]["manual"] is True
    assert [x["id"] for x in e["edits"]] == [40, 38]
    st2.apply_batch(e)
    snap = st2.snapshot()
    assert [l["id"] for l in snap] == [40, 60, 43], [l["id"] for l in snap]
    l40 = snap[0]
    assert l40["text"] == "벡터디비로 가죠" and l40["text_marked"] == "벡터디비로 가죠" and l40["low_confidence"] is False

    # 4) speakerLabel만 바뀐 edit은 건너뛴다 (예전에는 KeyError)
    only_label = {"meetingId": "m", "batch": 9, "lines": [], "edits": [{"seq": 40, "rev": 3, "speakerLabel": "B"}]}
    assert adapt_batch(only_label)["edits"] == []
    st2.apply_batch({"lines": [], "edits": [{"id": 40, "rev": 4, "speakerLabel": "B"}]})   # 저장소도 버티는지
    assert st2.snapshot()[0]["text"] == "벡터디비로 가죠"

    # 5) 이상한 입력은 이유를 밝히고 거절
    for bad in [{"meetingId": "m", "batch": 1, "lines": [{"seq": 1, "startMs": 10, "endMs": 5, "text": "x"}]},
                {"meetingId": "m", "batch": 1, "lines": [{"seq": "1", "startMs": 0, "endMs": 5, "text": "x"}]},
                {"batch": 1, "lines": []}]:
        try:
            adapt_batch(bad)
            raise SystemExit(f"잘못된 입력을 못 잡음: {bad}")
        except AdapterError:
            pass
    print("입력 어댑터 자체 시험 통과 (이벤트 4종, 줄 변환, edits, manual, speakerLabel, 잘못된 입력)")


if __name__ == "__main__":
    selftest()
