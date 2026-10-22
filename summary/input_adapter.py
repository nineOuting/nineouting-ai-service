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
  meetingId  문자열이나 정수(연동 API 초안의 공통 규칙은 숫자). 내부에서는 문자열(meeting_id)로 통일하고,
             백엔드로 돌려보낼 때 받은 타입 그대로 쓰도록 받은 값을 meeting_id_raw 에 둔다
  agendas      안건 목록 전체 (agenda-contract.md 4장). agendaId 는 문자열이나 정수, key 는 문자열
  corrections  사람이 고친 분류 (agenda-contract.md 7장). agendaId 가 null 이면 해당 없음
  attendees    안건 목록 요청의 선택 필드, 참석자 명단 (agenda-opinions-contract.md 6장). 없으면 None, 있으면 [{user_id, name}]
  item edits   사람이 고친 의견·할 일 (agenda-opinions-contract.md 5장). target 은 opinion 또는 todo

표준 라이브러리만 쓴다. 외부 호출 없음.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

RECOVERED_DELAY_MS = 2000                     # meeting_summary.py와 같은 값 (recovered 줄은 약 2초 뒤 도착)
EVENTS = ("start", "pause", "resume", "end")
OPINION_KINDS = ("OPINION", "EVIDENCE", "CONCERN", "QUESTION")   # 의견 항목의 종류 (agenda_opinions.py 와 같은 값)


class AdapterError(ValueError):
    """백엔드 입력이 형식과 다를 때."""


def _need(d, key, types, where):
    if key not in d:
        raise AdapterError(f"{where}: '{key}' 가 없음")
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, types):
        raise AdapterError(f"{where}: '{key}' 의 타입이 맞지 않음 ({type(v).__name__})")
    return v


def _meeting_id(msg, where):
    """meetingId 를 (내부용 문자열, 받은 값)으로 돌려준다. 문자열이나 정수만 받고 비어 있는 문자열도 그대로 둔다(기존 동작)."""
    v = _need(msg, "meetingId", (str, int), where)
    return str(v), v


def adapt_line(ln):
    where = f"line seq={ln.get('seq', '?')}"
    seq = _need(ln, "seq", int, where)
    start_ms = _need(ln, "startMs", int, where)
    # 백엔드 타입이 Long(null 가능)이라 endMs가 비어 오면 startMs로 대신한다 (문서 5.1)
    end_ms = start_ms if ln.get("endMs") is None else _need(ln, "endMs", int, where)
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
    meeting_id, raw_id = _meeting_id(msg, "batch")
    batch_no = _need(msg, "batch", int, "batch")
    lines = [adapt_line(x) for x in msg.get("lines") or []]
    edits = [a for a in (adapt_edit(x) for x in msg.get("edits") or []) if a is not None]
    return {"meeting_id": meeting_id, "meeting_id_raw": raw_id, "seq": batch_no, "lines": lines, "edits": edits}


def adapt_event(msg):
    """회의 이벤트(start / pause / resume / end)"""
    meeting_id, raw_id = _meeting_id(msg, "event")
    event = _need(msg, "event", str, "event")
    if event not in EVENTS:
        raise AdapterError(f"event: 알 수 없는 값 '{event}' (start, pause, resume, end 중 하나)")
    at_ms = _need(msg, "atMs", int, "event")
    return {"meeting_id": meeting_id, "meeting_id_raw": raw_id, "event": event, "at_ms": at_ms, "wall": msg.get("wall")}


def adapt_agendas(msg):
    """안건 목록 전체 (docs/api/agenda-contract.md 4장) -> {"meeting_id", "version", "agendas"}.
    agendaId 는 문자열이나 정수. 결과를 돌려줄 때 같은 타입을 쓰도록 받은 값(id)과 문자열 key 를 함께 둔다."""
    meeting_id, raw_id = _meeting_id(msg, "agendas")
    version = _need(msg, "version", int, "agendas")
    items = _need(msg, "agendas", list, "agendas")
    out, seen = [], set()
    for a in items:
        if not isinstance(a, dict):
            raise AdapterError("agendas: 항목이 객체가 아님")
        where = f"agenda {a.get('agendaId', '?')}"
        aid = _need(a, "agendaId", (str, int), where)
        key = str(aid)
        if not key.strip():
            raise AdapterError(f"{where}: agendaId가 비어 있음")
        if key in seen:
            raise AdapterError(f"{where}: agendaId가 겹침")
        seen.add(key)
        title = _need(a, "title", str, where)
        if not title.strip():
            raise AdapterError(f"{where}: title이 비어 있음")
        out.append({"id": aid, "key": key, "position": _need(a, "position", int, where), "title": title,
                    "decision_point": _need(a, "decisionPoint", str, where),
                    "added": bool(a.get("addedDuringMeeting", False))})
    attendees = None
    if msg.get("attendees") is not None:                     # 선택 필드. 없거나 null 이면 이전 명단을 그대로 둔다
        raw = msg["attendees"]
        if not isinstance(raw, list):
            raise AdapterError("agendas: 'attendees' 는 배열이어야 함")
        attendees = []
        for p in raw:
            if not isinstance(p, dict):
                raise AdapterError("attendees: 항목이 객체가 아님")
            where = f"attendee {p.get('userId', '?')}"
            uid = _need(p, "userId", (str, int), where)
            if isinstance(uid, str) and not uid.strip():
                raise AdapterError(f"{where}: userId가 비어 있음")
            name = _need(p, "name", str, where)
            if not name.strip():
                raise AdapterError(f"{where}: name이 비어 있음")
            attendees.append({"user_id": uid, "name": name.strip()})
    return {"meeting_id": meeting_id, "meeting_id_raw": raw_id, "version": version, "agendas": out, "attendees": attendees}


def adapt_corrections(msg):
    """사람이 고친 분류 (agenda-contract.md 7장) -> {"meeting_id", "corrections"}.
    agendaId 가 null 이면 '해당 없음'으로 고친 것이다. 키가 아예 없으면 실수로 보고 거절한다."""
    meeting_id, raw_id = _meeting_id(msg, "corrections")
    items = _need(msg, "corrections", list, "corrections")
    out = []
    for c in items:
        if not isinstance(c, dict):
            raise AdapterError("corrections: 항목이 객체가 아님")
        where = f"correction seq={c.get('seq', '?')}"
        seq = _need(c, "seq", int, where)
        rev = _need(c, "rev", int, where)
        if "agendaId" not in c:
            raise AdapterError(f"{where}: 'agendaId' 가 없음 (해당 없음이면 null)")
        aid = c["agendaId"] if c["agendaId"] is None else _need(c, "agendaId", (str, int), where)
        out.append({"seq": seq, "agenda_id": aid, "rev": rev})
    return {"meeting_id": meeting_id, "meeting_id_raw": raw_id, "corrections": out}


def adapt_summary_edits(msg):
    """사람이 고친 요약 (agenda-summary-contract.md 5장) -> {"meeting_id", "edits"}.
    edit 은 text 와 evidenceSeqs(1개 이상)가 필수이고, confirm 은 없어도 된다."""
    meeting_id, raw_id = _meeting_id(msg, "summary-edits")
    items = _need(msg, "edits", list, "edits")
    out = []
    for e in items:
        if not isinstance(e, dict):
            raise AdapterError("edits: 항목이 객체가 아님")
        where = f"summary edit itemId={e.get('itemId', '?')}"
        aid = _need(e, "agendaId", (str, int), where)
        iid = _need(e, "itemId", str, where)
        if not iid:
            raise AdapterError(f"{where}: 'itemId' 가 비어 있음")
        action = _need(e, "action", str, where)
        if action not in ("edit", "confirm"):
            raise AdapterError(f"{where}: 'action' 은 edit 또는 confirm 이어야 함 ({action})")
        rev = _need(e, "rev", int, where)
        row = {"agenda_id": aid, "item_id": iid, "action": action, "rev": rev}
        if action == "edit":
            text = _need(e, "text", str, where)
            if not text.strip():
                raise AdapterError(f"{where}: 'text' 가 비어 있음")
            ev = _need(e, "evidenceSeqs", list, where)
            if not ev or any(isinstance(x, bool) or not isinstance(x, int) for x in ev):
                raise AdapterError(f"{where}: 'evidenceSeqs' 는 1개 이상의 정수 배열이어야 함")
            row.update({"text": text, "evidence_seqs": ev})
        out.append(row)
    return {"meeting_id": meeting_id, "meeting_id_raw": raw_id, "edits": out}


def _opt_str(d, key, where):
    """선택 문자열 칸. 없거나 null 이거나 공백뿐이면 None."""
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, str):
        raise AdapterError(f"{where}: '{key}' 의 타입이 맞지 않음 ({type(v).__name__})")
    return v.strip() or None


def adapt_item_edits(msg):
    """사람이 고친 의견·할 일 (agenda-opinions-contract.md 5장) -> {"meeting_id", "edits"}.
    edit 은 evidenceSeqs(1개 이상)가 필수이고, 의견은 text·kind, 할 일은 task 가 필수다. confirm 은 없어도 된다."""
    meeting_id, raw_id = _meeting_id(msg, "item-edits")
    items = _need(msg, "edits", list, "edits")
    out = []
    for e in items:
        if not isinstance(e, dict):
            raise AdapterError("edits: 항목이 객체가 아님")
        where = f"item edit itemId={e.get('itemId', '?')}"
        aid = _need(e, "agendaId", (str, int), where)
        target = _need(e, "target", str, where)
        if target not in ("opinion", "todo"):
            raise AdapterError(f"{where}: 'target' 은 opinion 또는 todo 여야 함 ({target})")
        iid = _need(e, "itemId", str, where)
        if not iid:
            raise AdapterError(f"{where}: 'itemId' 가 비어 있음")
        action = _need(e, "action", str, where)
        if action not in ("edit", "confirm"):
            raise AdapterError(f"{where}: 'action' 은 edit 또는 confirm 이어야 함 ({action})")
        row = {"agenda_id": aid, "target": target, "item_id": iid, "action": action, "rev": _need(e, "rev", int, where)}
        if action == "edit":
            ev = _need(e, "evidenceSeqs", list, where)
            if not ev or any(isinstance(x, bool) or not isinstance(x, int) for x in ev):
                raise AdapterError(f"{where}: 'evidenceSeqs' 는 1개 이상의 정수 배열이어야 함")
            row["evidence_seqs"] = ev
            if target == "opinion":
                text = _need(e, "text", str, where)
                if not text.strip():
                    raise AdapterError(f"{where}: 'text' 가 비어 있음")
                kind = _need(e, "kind", str, where)
                if kind not in OPINION_KINDS:
                    raise AdapterError(f"{where}: 'kind' 는 {', '.join(OPINION_KINDS)} 중 하나여야 함 ({kind})")
                row.update({"text": text, "kind": kind})
            else:
                task = _need(e, "task", str, where)
                if not task.strip():
                    raise AdapterError(f"{where}: 'task' 가 비어 있음")
                uid = e.get("assigneeUserId")
                if uid is not None and (isinstance(uid, bool) or not isinstance(uid, (str, int))):
                    raise AdapterError(f"{where}: 'assigneeUserId' 의 타입이 맞지 않음 ({type(uid).__name__})")
                if isinstance(uid, str) and not uid.strip():
                    uid = None
                row.update({"task": task, "assignee_name": _opt_str(e, "assigneeName", where), "assignee_user_id": uid,
                            "due_text": _opt_str(e, "dueText", where)})
        out.append(row)
    return {"meeting_id": meeting_id, "meeting_id_raw": raw_id, "edits": out}


def adapt_offtopic_action(msg):
    """새 안건 제안·"지금 다른 이야기" 알림에 대한 사용자의 응답 (agenda-offtopic-contract.md 5장)
    -> {"meeting_id", "action", "rev", "at_ms"}. 지금은 dismiss(무시)만 받는다. atMs 는 선택이며 재시작 복구 때 무시한 시각(음성 시간)을 알려 준다."""
    meeting_id, raw_id = _meeting_id(msg, "off-topic-actions")
    action = _need(msg, "action", str, "off-topic-actions")
    if action != "dismiss":
        raise AdapterError(f"off-topic-actions: 'action' 은 dismiss 여야 함 ({action})")
    rev = _need(msg, "rev", int, "off-topic-actions")
    at_ms = msg.get("atMs")
    if at_ms is not None and (isinstance(at_ms, bool) or not isinstance(at_ms, int) or at_ms < 0):
        raise AdapterError("off-topic-actions: 'atMs' 는 0 이상의 정수여야 함")
    return {"meeting_id": meeting_id, "meeting_id_raw": raw_id, "action": action, "rev": rev, "at_ms": at_ms}


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
    assert e["lines"][0]["type"] == "final" and e["lines"][0]["manual"] is False
    nul = adapt_line({"seq": 62, "startMs": 5000, "endMs": None, "text": "끝 시각 없음"})
    assert nul["end_ms"] == 5000 and nul["arrival_ms"] == 5000
    man = adapt_line({"seq": 61, "startMs": 1, "endMs": 2, "text": "직접 추가", "manual": True})   # 지금은 오지 않지만 오더라도 처리
    assert man["type"] == "manual" and man["manual"] is True
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
    # 6) 안건 목록: 예시 파일, 타입 그대로, 거절할 입력
    g = adapt_agendas(load("agendas-put.json"))
    assert g["meeting_id"] == "m_1234" and g["version"] == 3 and [a["key"] for a in g["agendas"]] == ["a_101", "a_102", "a_103"]
    assert g["agendas"][2]["added"] is True and g["agendas"][0]["decision_point"].startswith("1차 배포")
    gi = adapt_agendas({"meetingId": "m", "version": 1, "agendas": [
        {"agendaId": 7, "position": 1, "title": "t", "decisionPoint": ""}]})
    assert gi["agendas"][0]["id"] == 7 and gi["agendas"][0]["key"] == "7" and gi["agendas"][0]["added"] is False
    assert adapt_agendas({"meetingId": "m", "version": 1, "agendas": []})["agendas"] == []
    ok = {"agendaId": "a", "position": 1, "title": "t", "decisionPoint": "d"}
    for bad in [{"meetingId": "m", "version": "1", "agendas": []},
                {"meetingId": "m", "agendas": []},
                {"meetingId": "m", "version": 1, "agendas": {}},
                {"meetingId": "m", "version": 1, "agendas": ["x"]},
                {"meetingId": "m", "version": 1, "agendas": [ok, dict(ok)]},                     # agendaId 겹침
                {"meetingId": "m", "version": 1, "agendas": [dict(ok, title="  ")]},
                {"meetingId": "m", "version": 1, "agendas": [dict(ok, agendaId=True)]},
                {"meetingId": "m", "version": 1, "agendas": [dict(ok, agendaId="")]},
                {"meetingId": "m", "version": 1, "agendas": [{k: v for k, v in ok.items() if k != "decisionPoint"}]},
                {"meetingId": "m", "version": 1, "agendas": [dict(ok, position="1")]}]:
        try:
            adapt_agendas(bad)
            raise SystemExit(f"잘못된 안건 목록을 못 잡음: {bad}")
        except AdapterError:
            pass
    # 7) 고친 분류: 예시 파일, null 은 해당 없음, agendaId 키 없음은 거절
    k = adapt_corrections(load("agenda-corrections-post.json"))
    assert k["corrections"] == [{"seq": 45, "agenda_id": "a_102", "rev": 1}, {"seq": 46, "agenda_id": None, "rev": 2}], k
    for bad in [{"meetingId": "m", "corrections": [{"seq": 1, "rev": 1}]},
                {"meetingId": "m", "corrections": [{"seq": "1", "agendaId": "a", "rev": 1}]},
                {"meetingId": "m", "corrections": [{"seq": 1, "agendaId": "a"}]},
                {"meetingId": "m", "corrections": [{"seq": 1, "agendaId": True, "rev": 1}]},
                {"meetingId": "m", "corrections": None}]:
        try:
            adapt_corrections(bad)
            raise SystemExit(f"잘못된 고침을 못 잡음: {bad}")
        except AdapterError:
            pass
    # 8) meetingId 는 문자열이나 정수. 내부는 문자열, 받은 값은 그대로 보관. 불리언·실수·null·없음은 거절
    for mid in ("m_1234", 31):
        want = (str(mid), mid)
        for got in (adapt_batch({"meetingId": mid, "batch": 1, "lines": []}),
                    adapt_event({"meetingId": mid, "event": "start", "atMs": 0}),
                    adapt_agendas({"meetingId": mid, "version": 1, "agendas": []}),
                    adapt_corrections({"meetingId": mid, "corrections": []})):
            assert (got["meeting_id"], got["meeting_id_raw"]) == want and isinstance(got["meeting_id"], str), got
    for bad in (True, 3.5, None):
        for fn, rest in ((adapt_batch, {"batch": 1, "lines": []}), (adapt_event, {"event": "start", "atMs": 0}),
                         (adapt_agendas, {"version": 1, "agendas": []}), (adapt_corrections, {"corrections": []})):
            try:
                fn(dict(rest, meetingId=bad))
                raise SystemExit(f"잘못된 meetingId 를 못 잡음: {bad!r} {fn.__name__}")
            except AdapterError:
                pass
    try:
        adapt_batch({"batch": 1, "lines": []})
        raise SystemExit("meetingId 가 없는 묶음을 받아 버림")
    except AdapterError:
        pass
    # 9) 사람이 고친 요약: 예시 파일, edit/confirm 필수 필드, 잘못된 입력
    se = adapt_summary_edits(load("agenda-summary-edits-post.json"))
    assert se["meeting_id"] == "31" and se["meeting_id_raw"] == 31 and len(se["edits"]) == 2
    e0, e1 = se["edits"]
    assert e0["action"] == "edit" and e0["agenda_id"] == 101 and e0["item_id"] == "k3f9a1-2" and e0["rev"] == 3
    assert e0["evidence_seqs"] == [44, 45] and e0["text"]
    assert e1["action"] == "confirm" and e1["rev"] == 4 and "text" not in e1 and "evidence_seqs" not in e1
    ok = {"meetingId": "m", "edits": [{"agendaId": "a", "itemId": "i", "action": "edit", "text": "글", "evidenceSeqs": [1], "rev": 1}]}
    assert adapt_summary_edits(ok)["edits"][0]["agenda_id"] == "a"
    assert adapt_summary_edits({"meetingId": "m", "edits": []})["edits"] == []

    def bad_edit(**change):
        e = dict(ok["edits"][0])
        for k, v in change.items():
            if v is _DROP:
                e.pop(k, None)
            else:
                e[k] = v
        try:
            adapt_summary_edits({"meetingId": "m", "edits": [e]})
        except AdapterError:
            return
        raise SystemExit(f"잘못된 고친 요약을 못 잡음: {change}")
    _DROP = object()
    for change in ({"action": "delete"}, {"text": _DROP}, {"text": "  "}, {"evidenceSeqs": _DROP}, {"evidenceSeqs": []},
                   {"evidenceSeqs": ["1"]}, {"evidenceSeqs": [True]}, {"rev": "1"}, {"rev": _DROP}, {"itemId": ""},
                   {"itemId": 5}, {"agendaId": None}, {"agendaId": _DROP}, {"action": _DROP}):
        bad_edit(**change)
    for bad in ({"meetingId": "m"}, {"meetingId": "m", "edits": {}}, {"meetingId": "m", "edits": ["x"]}, {"edits": []}):
        try:
            adapt_summary_edits(bad)
            raise SystemExit(f"잘못된 본문을 못 잡음: {bad}")
        except AdapterError:
            pass
    c1 = adapt_summary_edits({"meetingId": "m", "edits": [{"agendaId": "a", "itemId": "i", "action": "confirm", "rev": 1}]})
    assert c1["edits"][0]["action"] == "confirm"

    # 10) 참석자 명단: 선택 필드. 없거나 null 이면 None, 있으면 변환. 형식이 틀리면 거절
    ga = adapt_agendas(load("agendas-with-attendees-put.json"))
    assert ga["meeting_id_raw"] == 31 and ga["version"] == 4 and len(ga["agendas"]) == 3
    assert ga["attendees"] == [{"user_id": 7, "name": "김철수"}, {"user_id": 8, "name": "이영희"}], ga["attendees"]
    assert adapt_agendas(load("agendas-put.json"))["attendees"] is None, "필드가 없으면 None (이전 명단을 그대로 둔다)"
    base = {"meetingId": "m", "version": 1, "agendas": []}
    assert adapt_agendas(dict(base, attendees=None))["attendees"] is None
    assert adapt_agendas(dict(base, attendees=[]))["attendees"] == [], "빈 배열은 명단이 비었다는 뜻"
    assert adapt_agendas(dict(base, attendees=[{"userId": "u1", "name": " 김철수 "}]))["attendees"] == [{"user_id": "u1", "name": "김철수"}]
    for bad in ({"attendees": {}}, {"attendees": "x"}, {"attendees": ["x"]}, {"attendees": [{"name": "a"}]},
                {"attendees": [{"userId": 1}]}, {"attendees": [{"userId": True, "name": "a"}]},
                {"attendees": [{"userId": 1.5, "name": "a"}]}, {"attendees": [{"userId": "", "name": "a"}]},
                {"attendees": [{"userId": 1, "name": "  "}]}, {"attendees": [{"userId": 1, "name": 5}]}):
        try:
            adapt_agendas(dict(base, **bad))
            raise SystemExit(f"잘못된 참석자 명단을 못 잡음: {bad}")
        except AdapterError:
            pass
    # 11) 사람이 고친 의견·할 일: 예시 파일, 필수 칸, 선택 칸, 잘못된 입력
    ie = adapt_item_edits(load("agenda-item-edits-post.json"))
    assert ie["meeting_id"] == "31" and ie["meeting_id_raw"] == 31 and len(ie["edits"]) == 3
    o_edit, o_conf, t_edit = ie["edits"]
    assert o_edit == {"agenda_id": 101, "target": "opinion", "item_id": "k3f9a1-8", "action": "edit", "rev": 2,
                      "evidence_seqs": [45], "text": "QA는 수요일까지 가능하다는 설명", "kind": "EVIDENCE"}, o_edit
    assert o_conf == {"agenda_id": 101, "target": "opinion", "item_id": "k3f9a1-8", "action": "confirm", "rev": 3}, o_conf
    assert t_edit["target"] == "todo" and t_edit["task"] == "배포 체크리스트 작성" and t_edit["assignee_name"] == "이영희"
    assert t_edit["assignee_user_id"] == 8 and t_edit["due_text"] == "배포 전날까지" and t_edit["evidence_seqs"] == [47]
    okt = {"agendaId": "a", "target": "todo", "itemId": "i", "action": "edit", "task": "일", "evidenceSeqs": [1], "rev": 1}
    r = adapt_item_edits({"meetingId": "m", "edits": [okt]})["edits"][0]
    assert (r["assignee_name"], r["assignee_user_id"], r["due_text"]) == (None, None, None), "선택 칸은 없으면 None"
    r = adapt_item_edits({"meetingId": "m", "edits": [dict(okt, assigneeName="  ", dueText="", assigneeUserId="")]})["edits"][0]
    assert (r["assignee_name"], r["assignee_user_id"], r["due_text"]) == (None, None, None), "공백뿐인 값은 None"
    r = adapt_item_edits({"meetingId": "m", "edits": [dict(okt, assigneeName=None, assigneeUserId="u-3")]})["edits"][0]
    assert r["assignee_user_id"] == "u-3"
    oko = {"agendaId": "a", "target": "opinion", "itemId": "i", "action": "edit", "text": "글", "kind": "CONCERN", "evidenceSeqs": [1], "rev": 1}
    assert adapt_item_edits({"meetingId": "m", "edits": [oko]})["edits"][0]["kind"] == "CONCERN"
    assert adapt_item_edits({"meetingId": "m", "edits": []})["edits"] == []

    def bad_item(base, **change):
        e = dict(base)
        for k, v in change.items():
            if v is _DROP:
                e.pop(k, None)
            else:
                e[k] = v
        try:
            adapt_item_edits({"meetingId": "m", "edits": [e]})
        except AdapterError:
            return
        raise SystemExit(f"잘못된 고친 의견·할 일을 못 잡음: {change}")
    for change in ({"target": "summary"}, {"target": _DROP}, {"action": "delete"}, {"action": _DROP}, {"itemId": ""}, {"itemId": 5},
                   {"agendaId": None}, {"agendaId": _DROP}, {"rev": "1"}, {"rev": _DROP}, {"evidenceSeqs": _DROP},
                   {"evidenceSeqs": []}, {"evidenceSeqs": ["1"]}, {"evidenceSeqs": [True]}):
        bad_item(oko, **change)
        bad_item(okt, **change)
    for change in ({"text": _DROP}, {"text": "  "}, {"kind": _DROP}, {"kind": "MAYBE"}, {"kind": 5}):
        bad_item(oko, **change)
    for change in ({"task": _DROP}, {"task": "  "}, {"assigneeName": 5}, {"dueText": 5}, {"assigneeUserId": True},
                   {"assigneeUserId": 1.5}, {"assigneeUserId": []}):
        bad_item(okt, **change)
    for bad in ({"meetingId": "m"}, {"meetingId": "m", "edits": {}}, {"meetingId": "m", "edits": ["x"]}, {"edits": []}):
        try:
            adapt_item_edits(bad)
            raise SystemExit(f"잘못된 본문을 못 잡음: {bad}")
        except AdapterError:
            pass
    c2 = adapt_item_edits({"meetingId": "m", "edits": [{"agendaId": "a", "target": "todo", "itemId": "i", "action": "confirm", "rev": 1}]})
    assert c2["edits"][0]["action"] == "confirm" and "task" not in c2["edits"][0] and "evidence_seqs" not in c2["edits"][0]

    # 주제 변경 제안에 대한 응답
    a = adapt_offtopic_action({"meetingId": 7, "action": "dismiss", "rev": 3})
    assert a == {"meeting_id": "7", "meeting_id_raw": 7, "action": "dismiss", "rev": 3, "at_ms": None}, a
    assert adapt_offtopic_action({"meetingId": "m", "action": "dismiss", "rev": 1, "atMs": 0})["at_ms"] == 0
    for bad in ({"meetingId": "m", "action": "dismiss"}, {"meetingId": "m", "rev": 1}, {"action": "dismiss", "rev": 1},
                {"meetingId": "m", "action": "register", "rev": 1}, {"meetingId": "m", "action": "dismiss", "rev": "1"},
                {"meetingId": "m", "action": "dismiss", "rev": True}, {"meetingId": "m", "action": "dismiss", "rev": 1, "atMs": -1},
                {"meetingId": "m", "action": "dismiss", "rev": 1, "atMs": "5"}, {"meetingId": "m", "action": "dismiss", "rev": 1, "atMs": 1.5}):
        try:
            adapt_offtopic_action(bad)
            raise SystemExit(f"잘못된 무시 요청을 못 잡음: {bad}")
        except AdapterError:
            pass

    print("입력 어댑터 자체 시험 통과 (이벤트 4종, 줄 변환, edits, manual, speakerLabel, 잘못된 입력, 안건 목록, 고친 분류, meetingId 문자열·정수, 고친 요약, "
          "참석자 명단, 고친 의견·할 일, 주제 변경 무시 요청)")


if __name__ == "__main__":
    selftest()
