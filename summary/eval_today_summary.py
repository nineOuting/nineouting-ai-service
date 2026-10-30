#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
오늘 회의 요약 정리본 정합성 점검 (AI-08)

모델을 부르지 않는다 (비용 0원). 사람이 확정한 정답(의견·할 일 정답)과 안건별 최종 상태 정답으로 안건 보드를 만들어
정리본(agenda_today_summary.build_today_summary)에 넣고, 조합이 틀리지 않았는지 점검한다. 요약 품질은 재지 않는다.

입력 보드를 이렇게 만든다
  의견·할 일: 확정된 정답(*_items_truth.json) 그대로. 애매하다고 표시한 항목은 확인 필요(needsCheck)로 넣는다.
  안건별 상태: *_status_truth.json 의 end 상태. 확정 전(confirmed false)이면 --use-draft 가 있어야 쓰며, 이때 상태에 기대는 점검 결과는 참고용이다.
  안건별 요약 문장: 사람이 확정한 요약 정답이 없어 시험용 자리표시 문장을 쓴다 (복사가 정확한지만 보면 되므로 내용은 상관없다).

점검 항목 (통과 기준은 모두 0건. 실행 전에 정했고 결과를 본 뒤 바꾸지 않는다)
  1. agendaSet       정리본의 안건이 보드의 안건과 다른 수 (빠짐, 더해짐, 순서 다름)
  2. statusMismatch  안건 상태가 정답 상태(종료를 가정)와 다른 수
  3. extraFields     안건 칸이 정해진 여섯 칸(agendaId, status, statusReason, summary, opinions, todos)과 다른 수 (결정 문장 같은 새 값이나 표시용 칸이 더해진 경우)
  4. fidelity        정리본의 판정 근거·요약 문장·의견·할 일이 원래 값과 다른 수 (새로 만들거나 바뀐 값, 빠진 값 포함)
  5. carryOver       결정이 아닌 안건의 상태가 정답 상태로 계산한 이월 후보(안건·순서·상태·근거·요약)와 다르거나 미결 안건 목록(leftover)과 다른 수
  6. copyBroken      입력을 나중에 바꿨을 때 이미 만든 정리본이 따라 바뀐 수
  7. notRepeatable   같은 입력으로 두 번 만든 결과가 다른 수
  8. headerMismatch  머리값(requestRev, final)이 입력과 다른 수 (요청 중, 요청 없이 끝, 요청 뒤 끝 세 경우)
  (안건별 묶음으로 바꾸기 전에는 결정/이월/할 일 세 덩어리를 점검했고 그 결과 파일은 result_today_summary_v1.json 이다. 지금 항목은 새 모양에 맞춘 것이고 결과는 v2 에 저장한다.
   이전 모양에서 8번(머리값)은 점검 도구에 일부러 오류를 넣어 보다가 못 잡는 것을 알고 추가한 것이다.)

한계: 확정된 정답(회의 A·B)에는 할 일의 담당자 이름·기한이 하나도 없다. 그래서 이름·기한 값이 그대로 복사되는지는 이 데이터로는 점검되지 않는다
  (결과의 info 에 이름·기한이 있는 할 일 수를 남긴다). 그 부분은 모듈 자체 시험의 합성 할 일로 확인한다.

결과 JSON 에는 전사와 문장을 넣지 않는다 (개수와 통과 여부만).

사용
  python3 eval_today_summary.py \\
    --meeting A,../data/eval_v2/a_agendas.json,../data/eval_v2/a_items_truth.json,../data/eval_v2/a_status_truth.json \\
    --meeting B,../data/eval_v2/b_agendas.json,../data/eval_v2/b_items_truth.json,../data/eval_v2/b_status_truth.json \\
    --use-draft --out ../data/eval_v2/result_today_summary_v2.json
"""
import argparse
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from agenda_leftover import build_leftover, LEFTOVER_STATUSES  # noqa: E402
from agenda_today_summary import build_today_summary  # noqa: E402

CRITERIA = {k: 0 for k in ("agendaSet", "statusMismatch", "extraFields", "fidelity", "carryOver", "copyBroken", "notRepeatable", "headerMismatch")}
STATUS_BASED = ("statusMismatch", "carryOver")                          # 상태 정답에 기대는 점검 (상태 정답이 초안이면 참고용)
AGENDA_FIELDS = {"agendaId", "status", "statusReason", "summary", "opinions", "todos"}


def _blank(v):
    return not (isinstance(v, str) and v.strip())


def build_inputs(agendas, items_truth, status_truth):
    """정답으로 보드의 안건 항목(ents)과 종료 확정을 가정한 상태(status_info)를 만든다."""
    order = [str(a["agendaId"]) for a in sorted(agendas["agendas"], key=lambda a: a["position"])]
    ents, status_info = [], {}
    for aid in order:
        tr = items_truth["agendas"].get(aid, {"opinions": [], "todos": []})
        seqs = sorted({s for it in tr["opinions"] for s in it["evidence"]})
        half = (len(seqs) + 1) // 2
        summary = [{"id": f"{aid}-s{i}", "text": f"[시험용 요약 {aid}-{i}]", "evidenceSeqs": part, "edited": i == 0, "needsCheck": i == 1}
                   for i, part in enumerate([seqs[:half], seqs[half:]]) if part]
        opinions = [{"id": f"{aid}-o{o['id']}", "kind": o["kind"], "text": o["text"], "evidenceSeqs": list(o["evidence"]),
                     "edited": False, "needsCheck": bool(o.get("unsure"))} for o in tr["opinions"]]
        todos = [{"id": f"{aid}-t{t['id']}", "task": t["task"], "assigneeName": t["assigneeName"],
                  "assigneeStatus": "MATCHED" if not _blank(t["assigneeName"]) else "UNASSIGNED", "assigneeUserId": None,
                  "dueText": t["dueText"], "evidenceSeqs": list(t["evidence"]), "edited": False, "needsCheck": bool(t.get("unsure")),
                  "unclear": False} for t in tr["todos"]]
        ents.append((aid, {"agendaId": aid, "summary": summary, "opinions": opinions, "todos": todos}))
        st = status_truth["end"]["statuses"][aid]
        status = "UNDECIDED" if st["status"] == "DISCUSSING" else st["status"]       # 종료를 가정하면 논의 중은 미결이 된다
        seq = list(st.get("evidenceSeqs") or [])
        status_info[aid] = {"status": status, "reason": {"text": "[시험용 판정 근거]", "evidenceSeqs": seq} if seq else None}
    return order, ents, status_info


def _scramble(x):
    """입력을 나중에 바꾸는 시험용: 문자열과 목록을 모두 바꾼다."""
    if isinstance(x, dict):
        for k, v in list(x.items()):
            if isinstance(v, str):
                x[k] = v + "(바뀜)"
            elif isinstance(v, (dict, list)):
                _scramble(v)
    elif isinstance(x, list):
        x.append(-1)
        for v in x:
            if isinstance(v, (dict, list)):
                _scramble(v)


def check(order, ents, status_info, meta):
    out = build_today_summary(meta, ents)
    snapshot = copy.deepcopy(out)
    stat = {a: status_info[a]["status"] for a in order}
    src = dict(ents)
    res = {k: 0 for k in CRITERIA}

    ids = [x["agendaId"] for x in out["agendas"]]
    res["agendaSet"] = sum(1 for g, e in zip(ids, order) if g != e) + abs(len(ids) - len(order))
    got = {x["agendaId"]: x for x in out["agendas"]}
    for a in order:
        x = got.get(a)
        if x is None:
            continue
        res["extraFields"] += int(set(x) != AGENDA_FIELDS)
        res["statusMismatch"] += int(x.get("status") != stat[a])
        e = src[a]
        res["fidelity"] += (x.get("statusReason") != status_info[a]["reason"]) + (x.get("summary") != e["summary"]) \
            + (x.get("opinions") != e["opinions"]) + (x.get("todos") != e["todos"])

    exp_carry = []                                             # 5) 이월 후보: 정답 상태에서 따로 계산
    for a in order:
        if stat[a] in LEFTOVER_STATUSES:
            exp_carry.append({"agendaId": a, "status": stat[a], "statusReason": status_info[a]["reason"], "summary": src[a]["summary"]})
    lf = build_leftover(meta, ents)["agendas"]
    non_decided = [{"agendaId": x["agendaId"], "status": x["status"], "statusReason": x.get("statusReason"), "summary": x.get("summary")}
                   for x in out["agendas"] if x["status"] in LEFTOVER_STATUSES]
    res["carryOver"] = (non_decided != exp_carry) + (non_decided != lf)

    keep_inputs = copy.deepcopy((ents, status_info))           # 6) 입력을 바꿔도 이미 만든 값은 그대로인가 (입력은 시험 뒤 복원)
    _scramble([e for _a, e in ents])
    _scramble(list(status_info.values()))
    res["copyBroken"] = int(out != snapshot)
    ents[:], status_info_restore = keep_inputs[0], keep_inputs[1]
    status_info.clear()
    status_info.update(status_info_restore)
    res["notRepeatable"] = int(build_today_summary(meta, ents) != snapshot)
    for rev, final in ((1, False), (None, True), (7, True)):
        o2 = build_today_summary({**meta, "requestRev": rev, "final": final}, ents)
        res["headerMismatch"] += (o2["requestRev"] != rev) + (o2["final"] is not final)

    todos = [t for x in out["agendas"] for t in x["todos"]]
    info = {"agendas": len(out["agendas"]), "decided": sum(1 for x in out["agendas"] if x["status"] == "DECIDED"),
            "carryOver": len(non_decided), "carryOverDecisionCheck": sum(1 for x in non_decided if x["status"] == "DECISION_CHECK"),
            "todos": len(todos), "todosAssigneeBlank": sum(1 for t in todos if _blank(t["assigneeName"])),
            "todosDueBlank": sum(1 for t in todos if _blank(t["dueText"])),
            "todosWithNameOrDue": sum(1 for t in todos if not (_blank(t["assigneeName"]) and _blank(t["dueText"]))),
            "opinions": sum(len(x["opinions"]) for x in out["agendas"]), "jsonOk": bool(json.dumps(out))}
    return res, info


def main():
    ap = argparse.ArgumentParser(description="오늘 회의 요약 정리본 정합성 점검 (모델 호출 없음, 비용 0원)")
    ap.add_argument("--meeting", action="append", required=True, help="이름,안건목록.json,의견할일정답.json,상태정답.json (여러 번)")
    ap.add_argument("--use-draft", action="store_true", help="확정 전 상태 정답 초안을 쓴다 (상태에 기대는 점검은 참고용)")
    ap.add_argument("--out", help="결과 JSON 경로 (이미 있으면 거부)")
    a = ap.parse_args()
    if a.out and os.path.exists(a.out):
        sys.exit(f"결과 파일이 이미 있습니다: {a.out} (같은 데이터로 다시 재지 않습니다. 이름을 바꿔 두고 다시 실행하세요)")
    report = {"cost": "0원 (모델 호출 없음)", "criteria": CRITERIA, "meetings": {}}
    all_ok = True
    for spec in a.meeting:
        name, p_ag, p_it, p_st = spec.split(",")
        ag, it, st = (json.load(open(p, encoding="utf-8")) for p in (p_ag, p_it, p_st))
        if it.get("confirmed") is not True:
            sys.exit(f"{name}: 의견·할 일 정답이 확정되지 않았습니다. 확정된 정답만 씁니다.")
        draft = st.get("confirmed") is not True
        if draft and not a.use_draft:
            sys.exit(f"{name}: 상태 정답이 확정 전입니다. 참고용으로 쓰려면 --use-draft 를 붙이세요.")
        order, ents, status_info = build_inputs(ag, it, st)
        meta = {"requestRev": 1, "final": False, "status_info": status_info}
        res, info = check(order, ents, status_info, meta)
        ok = {k: res[k] <= CRITERIA[k] for k in CRITERIA}
        report["meetings"][name] = {"statusTruthConfirmed": not draft, "agendas": len(order), "violations": res, "pass": ok, "info": info}
        shown = []
        for k in CRITERIA:
            tag = " (참고용)" if draft and k in STATUS_BASED else ""
            shown.append(f"{k}={res[k]}{'' if ok[k] else ' 미달'}{tag}")
        print(f"[{name}] 안건 {len(order)}개 / " + " ".join(shown))
        print(f"      정리본: 결정 {info['decided']}, 결정이 아닌 안건 {info['carryOver']}(확인 필요 {info['carryOverDecisionCheck']}), 할 일 {info['todos']}"
              f"(담당자 비어 있음 {info['todosAssigneeBlank']}, 기한 비어 있음 {info['todosDueBlank']})")
        all_ok = all_ok and all(ok.values())
    report["allPass"] = all_ok
    print("전체:", "통과 (모든 항목 0건)" if all_ok else "미달 항목 있음")
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        json.dump(report, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("결과 저장:", a.out)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
