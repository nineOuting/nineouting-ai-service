#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
오늘 회의 요약 정리본 (AI-08)

회의를 끝내기 전에 모두가 같은 결론을 보도록, 회의 중 보였던 결과(안건별 요약, 상태, 의견, 할 일)를
안건 보드의 todaySummary 칸에 안건별로 묶어 담는다. 형식은 docs/api/agenda-today-summary-contract.md.
새로 요약하지 않는다. 보드에 이미 있는 값을 복사해 담을 뿐이고, 모델은 부르지 않는다 (비용 0).
회의록 초안(AI-10)이 이 칸을 그대로 재료로 쓴다 (결정 문장은 초안이 DECIDED 안건의 statusReason 으로 만든다. 여기서 만들지 않는다).

agendas[] : 보드의 agendas[] 순서 그대로, 안건마다
  agendaId, status, statusReason, summary[], opinions[], todos[]
  status 는 종료 확정을 가정한 값(AgendaStatusJudge.board_status(preview_final=True))이다. 그래서 DISCUSSING 은 오지 않는다(UNDECIDED 로 바뀐다).
  이월 후보는 status 로 알 수 있다(UNDECIDED, NOT_STARTED, DECISION_CHECK). 이월 목록과 논의 요약은 leftover 칸이 따로 담는다.
  담당자·기한 미정은 assigneeName 이나 dueText 가 비어 있는 것으로 안다 (표시용 칸을 더하지 않는다).

시점은 leftover 칸과 같다. 종료 버튼 요청 뒤부터, 이어서 논의하기를 받으면 거두고, 회의가 끝나면 마지막 값(final: true).
값은 모두 복사본이다. 이후 보드가 바뀌어도 이 칸의 값은 그 시점 그대로다.
로그 규칙: 전사 내용, 안건 제목, 문장을 남기지 않는다. 번호와 개수만 남긴다.
"""
import copy

DECIDED = "DECIDED"


def build_today_summary(meta, ents):
    """정리본을 만든다.
    meta: {"requestRev", "final", "status_info"}  status_info 는 안건 key -> {"status", "reason"} (종료 확정을 가정한 값)
    ents: [(안건 key, 보드의 안건 항목)] 보드의 agendas[] 순서. 항목에 summary, opinions, todos 가 있어야 한다.
    상태가 없는 안건은 담지 않는다 (보드는 모든 안건의 상태를 내므로 실제로는 없는 경우)."""
    info = meta["status_info"]
    agendas = []
    for key, ent in ents:
        si = info.get(key)
        if si is None:
            continue
        r = si.get("reason")
        agendas.append({"agendaId": ent["agendaId"], "status": si["status"],
                        "statusReason": {"text": r["text"], "evidenceSeqs": list(r["evidenceSeqs"])} if r else None,
                        "decisionPoints": copy.deepcopy(ent.get("decisionPoints", [])),
                        "summary": copy.deepcopy(ent["summary"]),
                        "opinions": copy.deepcopy(ent["opinions"]),
                        "todos": copy.deepcopy(ent["todos"])})
    return {"requestRev": meta["requestRev"], "final": bool(meta["final"]), "agendas": agendas}


def counts(ts):
    """상태 조회용 개수. 내용은 담지 않는다."""
    if ts is None:
        return {"agendas": 0, "decided": 0, "todos": 0}
    return {"agendas": len(ts["agendas"]), "decided": sum(1 for a in ts["agendas"] if a["status"] == DECIDED),
            "todos": sum(len(a["todos"]) for a in ts["agendas"])}


# --------------------------------------------------------------------------------------
def selftest():
    def summ(aid, texts=()):
        return [{"id": f"s{aid}-{i}", "text": t, "evidenceSeqs": [i + 1], "edited": i == 0, "needsCheck": False} for i, t in enumerate(texts)]

    def op(aid, texts=()):
        return [{"id": f"o{aid}-{i}", "kind": "OPINION", "text": t, "evidenceSeqs": [i + 2], "edited": False, "needsCheck": i == 0} for i, t in enumerate(texts)]

    def todo(tid, task, name=None, status=None, due=None, uid=None):
        return {"id": tid, "task": task, "assigneeName": name, "assigneeStatus": status, "assigneeUserId": uid, "dueText": due,
                "evidenceSeqs": [5], "edited": False, "needsCheck": False, "unclear": False}

    def dp(i, status, text, ev=(), edited=False, need=False):
        return {"decisionPointId": i, "status": status, "decisionText": text, "evidenceSeqs": list(ev), "edited": edited, "needsCheck": need}

    def ent(aid, s=(), o=(), t=(), d=()):
        return {"agendaId": aid, "decisionPoints": list(d), "summary": summ(aid, s), "opinions": op(aid, o), "todos": list(t)}

    why = lambda t, *s: {"text": t, "evidenceSeqs": list(s)}
    status = {"k1": {"status": "DECIDED", "reason": why("목요일로 확정", 3)},
              "k2": {"status": "UNDECIDED", "reason": why("미결", 9)},
              "k3": {"status": "NOT_STARTED", "reason": None},
              "k4": {"status": "DECISION_CHECK", "reason": why("확인", 12, 13)},
              "k5": {"status": "DECIDED", "reason": None}}
    ents = [("k1", ent(101, ["a", "a2"], ["o1"], [todo("t1", "일정 공유", "철수", "MATCHED", "수요일까지", 7), todo("t2", "표 만들기")],
                              [dp(7, "DECIDED", "목요일로 정함", [3, 4]), dp(8, "DECISION_CHECK", "담당 후보", [5], edited=True, need=True)])),
            ("k2", ent(102, ["b"], ["o2"], [todo("t3", "예산안 검토", "민수", "NOT_IN_ROSTER", None)],
                       [dp(9, "UNDECIDED", "점검 항목은 정해지지 않음", [])])),
            ("k3", ent(103)),
            ("k4", ent(104, ["d"], [], [todo("t4", "QA 담당 확인", "  ", "UNASSIGNED", "   ")])),
            ("k5", ent(105, ["e"])),
            ("k7", ent(107, ["g"]))]                                                # k7 은 상태가 없다
    meta = {"requestRev": 4, "final": False, "status_info": status}
    out = build_today_summary(meta, ents)

    # 1) 머리값과 안건별 묶음: 보드 순서, 상태가 있는 안건 모두, 안건마다 같은 여섯 칸
    assert set(out) == {"requestRev", "final", "agendas"} and out["requestRev"] == 4 and out["final"] is False
    assert [a["agendaId"] for a in out["agendas"]] == [101, 102, 103, 104, 105], "보드 순서, 상태 없는 107 은 없다"
    assert all(set(a) == {"agendaId", "status", "statusReason", "decisionPoints", "summary", "opinions", "todos"} for a in out["agendas"])
    assert "decision" not in out["agendas"][0], "안건 단위 결정 문장(decision)은 없다. 결정 문장은 사항별 decisionText 다"
    assert [a["status"] for a in out["agendas"]] == ["DECIDED", "UNDECIDED", "NOT_STARTED", "DECISION_CHECK", "DECIDED"]
    assert out["agendas"][0]["statusReason"] == why("목요일로 확정", 3) and out["agendas"][2]["statusReason"] is None and out["agendas"][4]["statusReason"] is None
    assert out["agendas"][3]["statusReason"] == why("확인", 12, 13)

    # 2) 요약, 의견, 할 일은 원래 값 그대로 (edited, needsCheck, 근거 줄 포함). 결정이 아닌 안건에도 의견, 할 일이 있다
    a0, a1 = out["agendas"][0], out["agendas"][1]
    assert a0["summary"] == ents[0][1]["summary"] and a0["opinions"] == ents[0][1]["opinions"] and a0["todos"] == ents[0][1]["todos"]
    assert [o["text"] for o in a1["opinions"]] == ["o2"] and [t["id"] for t in a1["todos"]] == ["t3"], "미결 안건의 의견, 할 일도 담는다"
    assert out["agendas"][2]["summary"] == [] and out["agendas"][2]["opinions"] == [] and out["agendas"][2]["todos"] == []
    assert set(a0["todos"][0]) == {"id", "task", "assigneeName", "assigneeStatus", "assigneeUserId", "dueText", "evidenceSeqs", "edited", "needsCheck", "unclear"}, \
        "담당자·기한 미정 표시 칸을 더하지 않는다"
    assert a0["todos"][1]["assigneeName"] is None and a0["todos"][1]["dueText"] is None, "미정은 null 로 안다"
    assert a1["todos"][0]["assigneeName"] == "민수" and a1["todos"][0]["assigneeStatus"] == "NOT_IN_ROSTER", "명단과 못 맞춘 이름도 그대로"
    # 2-1) 결정할 사항별 결과(AI-04)도 보드 값 그대로: 결정, 결정 확인 필요, 미결, 고친 표시, 근거 줄 (빈 근거도 그대로)
    assert a0["decisionPoints"] == ents[0][1]["decisionPoints"] and [p["status"] for p in a0["decisionPoints"]] == ["DECIDED", "DECISION_CHECK"]
    assert a0["decisionPoints"][1]["edited"] is True and a0["decisionPoints"][1]["needsCheck"] is True
    assert a1["decisionPoints"][0]["evidenceSeqs"] == [] and out["agendas"][2]["decisionPoints"] == []
    assert set(a0["decisionPoints"][0]) == {"decisionPointId", "status", "decisionText", "evidenceSeqs", "edited", "needsCheck"}

    # 3) 복사본: 원본이나 결과를 바꿔도 서로 영향이 없다
    ents[0][1]["summary"][0]["text"] = "바뀜"
    ents[0][1]["opinions"][0]["evidenceSeqs"].append(99)
    ents[0][1]["todos"][0]["task"] = "바뀜"
    ents[0][1]["decisionPoints"][0]["evidenceSeqs"].append(99)
    status["k1"]["reason"]["evidenceSeqs"].append(99)
    assert a0["summary"][0]["text"] == "a" and a0["opinions"][0]["evidenceSeqs"] == [2]
    assert a0["todos"][0]["task"] == "일정 공유" and a0["statusReason"]["evidenceSeqs"] == [3]
    assert a0["decisionPoints"][0]["evidenceSeqs"] == [3, 4], "결정할 사항도 복사해 담는다"
    a0["todos"][1]["task"] = "결과를 바꿔 봄"
    assert ents[0][1]["todos"][1]["task"] == "표 만들기", "결과를 바꿔도 원본은 그대로"

    # 4) 끝(final), 요청 없이 끝난 경우의 requestRev, 비어 있는 경우
    fin = build_today_summary({"requestRev": None, "final": True, "status_info": {"k1": {"status": "UNDECIDED", "reason": why("끝", 7)}}},
                              [("k1", ent(101, ["a"], [], [todo("t1", "x")]))])
    assert fin == {"requestRev": None, "final": True, "agendas": [
        {"agendaId": 101, "status": "UNDECIDED", "statusReason": why("끝", 7), "decisionPoints": [], "summary": summ(101, ["a"]), "opinions": [], "todos": [todo("t1", "x")]}]}, fin
    empty = build_today_summary({"requestRev": 1, "final": False, "status_info": {}}, [])
    assert empty == {"requestRev": 1, "final": False, "agendas": []}
    assert counts(out) == {"agendas": 5, "decided": 2, "todos": 4} and counts(None) == {"agendas": 0, "decided": 0, "todos": 0}
    import json
    json.dumps(out)
    print("AI-08 오늘 회의 요약 정리본 자체 시험 통과 (안건별 묶음, 결정할 사항별 결과, 결정 문장 없음, 복사본, 끝, 빈 결과)")


if __name__ == "__main__":
    selftest()
