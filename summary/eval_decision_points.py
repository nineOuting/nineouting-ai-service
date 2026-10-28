#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
결정할 사항 단위 안건 상태 판정 평가 (AI-04 v0.2)

끝난 회의 전사를 실제 서비스처럼 10초 묶음으로 흘려 보내며, 정해 둔 체크포인트와 종료 때의 **결정할 사항별 상태**와 안건 상태를
사람이 확정한 정답과 맞춰 본다. 분류는 사람이 확정한 라벨을 그대로 쓴다(gold). 분류 모델은 부르지 않고 판정기 품질만 잰다.
설계와 기준: 프로젝트 문서 claude/ai04-decision-points-eval-design-v1.md (통과 기준은 실행 전에 확정, 결과를 본 뒤 바꾸지 않는다).

재는 것 (M1~M7)
  M1 사항 상태 일치율, 전체 칸                                   기준 0.80 이상
  M2 사항 상태 일치율, 정답이 논의 전이 아닌 칸                    기준 0.70 이상
  M3 잘못된 결정: 종료 때 정답이 결정이 아닌 사항을 결정으로 낸 수     기준 0
  M4 결정 누락: 종료 때 정답이 결정인 사항을 결정으로 못 낸 수        기준 2 이하
  M5 안건 상태 일치율 (모델 출력으로 도출한 안건 상태)               기준 0.85 이상
  M6 근거 줄이 그 사항의 정답 근거 범위 안에 있는 비율 (결정, 결정 확인 필요)  기준 0.90 이상
  M7 지어낸 문장 0 : 사람이 채점한다. --texts-out 이 채점표를 만든다 (결과 JSON 에는 문장을 넣지 않는다)
참고 보고(기준 없음): 정답이 결정이 아닌 칸을 체크포인트마다 결정으로 낸 수, 고정 문구로 간 사항 수, 검증 탈락 수, 호출 수와 비용
자동 점검(기대 100%): 결정·결정 확인 필요 사항의 근거가 비어 있지 않음, 사항 번호가 안건 목록과 같음

사용
  # 모델 호출 없이 호출 수와 비용 추정만 (비용 0)
  python3 eval_decision_points.py --meeting D1,d1.jsonl,d1_agendas.json,d1_labels_confirmed.tsv,d1_points_truth.json --meeting D2,... --dry-run --provider gemini --model gemini-3.8-flash
  # 에코 모의 판정기로 스크립트 점검 (비용 0)
  python3 eval_decision_points.py --meeting ... --provider echo --use-draft
  # 유료 평가 (한 번만. 결과 파일이 이미 있으면 거부) -- 키는 저장소 루트 .env 에서 읽는다
  python3 eval_decision_points.py --meeting ... --provider gemini --model gemini-3.8-flash --min-new-lines 8 --paid-ok --out R.json --texts-out S.txt
  python3 eval_decision_points.py --selftest      # 채점 계산 자체 시험 (비용 0)
정답 파일은 사람이 확정해야 한다. confirmed 가 true 가 아니면 --use-draft 일 때만 쓴다 (점검용, 최종 수치 아님).
"""
import argparse
import copy
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from meeting_summary import load_jsonl_as_protocol_lines, make_batches, mmss  # noqa: E402
import agenda_status as ast  # noqa: E402
import eval_agenda as ea  # noqa: E402
import eval_agenda_summary as es  # noqa: E402
import eval_agenda_status as eas  # noqa: E402

CRITERIA = {                         # 설계서 5장의 기준. 실행 전에 확정했고 결과를 본 뒤 바꾸지 않는다
    "pointAgreementAll": 0.80,       # M1
    "pointAgreementNonNotStarted": 0.70,   # M2
    "maxFalseDecided": 0,            # M3
    "maxMissedDecisions": 2,         # M4
    "agendaAgreement": 0.85,         # M5
    "evidenceInRange": 0.90,         # M6
    "fabricatedSentences": 0,        # M7 (사람이 채점)
}
OUT_TOK_BASE = 25                    # 호출 한 번의 출력 중 사항 수와 무관한 부분 (JSON 틀)
OUT_TOK_PER_POINT = 60               # 사항 하나의 출력 (n, verdict, 문장 한 줄, 근거) 추정


def load_truth(path, use_draft):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not d.get("confirmed") and not use_draft:
        sys.exit(f"정답 사항 파일이 아직 확정되지 않았습니다 ({os.path.basename(path)}: confirmed 가 true 가 아님). 사람이 확인한 뒤 true 로 바꾸세요. 점검용이면 --use-draft")
    return d, bool(d.get("confirmed"))


def replay_points(d):
    return [{"id": c["id"], "atMs": c["atMs"]} for c in d["checkpoints"]] + [{"id": "END", "atMs": None}]


# --------------------------------------------------------------------------------------
# 채점
# --------------------------------------------------------------------------------------
def score_meeting(snaps, truth, point_text):
    """snaps: 체크포인트 id -> 안건 id -> {"status", "reason", "points": [...]} (AgendaStatusJudge.board_status 를 안건 id 로 바꾼 것).
    반환: 합칠 수 있는 개수들(counts)과 칸별 결과(cells, 상태만)."""
    cps = list(truth["checkpoints"]) + [truth["end"]]
    agenda_of = {p["id"]: p["agendaId"] for p in truth["points"]}
    rng = {}                                              # 사항 -> 정답 근거 범위 (결정, 결정 확인 필요 칸의 evidenceRange 를 합친 것)
    for c in cps:
        for pid, t in c["points"].items():
            if t.get("evidenceRange"):
                lo, hi = t["evidenceRange"]
                a, b = rng.get(pid, (lo, hi))
                rng[pid] = (min(a, lo), max(b, hi))
    k = dict(cells=0, ok=0, ok_lenient=0, ns_cells=0, ns_ok=0, nonns_cells=0, nonns_ok=0, agenda_cells=0, agenda_ok=0,
             ev_lines=0, ev_in=0, auto_missing_point=0, auto_empty_evidence=0, fixed_phrases=0, unknown_points=0)
    missed, false_end, false_cells, cells = [], [], [], {}
    for c in cps:
        got = snaps[c["id"]]
        row = {}
        for pid, t in c["points"].items():
            aid = agenda_of[pid]
            pts = {p["decisionPointId"]: p for p in got.get(aid, {}).get("points", [])}
            if pid not in pts:
                k["auto_missing_point"] += 1
                continue
            pp = pts[pid]
            pred, ok_strict = pp["status"], pp["status"] == t["status"]
            ok_len = ok_strict or pred in t.get("alsoOk", [])
            k["cells"] += 1; k["ok"] += ok_strict; k["ok_lenient"] += ok_len
            if t["status"] == "NOT_STARTED":
                k["ns_cells"] += 1; k["ns_ok"] += ok_strict
            else:
                k["nonns_cells"] += 1; k["nonns_ok"] += ok_strict
            if pred == "DECIDED" and t["status"] in ("UNDECIDED", "DECISION_CHECK") and "DECIDED" not in t.get("alsoOk", []):
                false_cells.append(f"{c['id']}:{pid}")
            if c["id"] == "END":
                if t["status"] == "DECIDED" and pred != "DECIDED":
                    missed.append(pid)
                if pred == "DECIDED" and t["status"] != "DECIDED" and "DECIDED" not in t.get("alsoOk", []):
                    false_end.append(pid)
            if pred in ("DECIDED", "DECISION_CHECK"):
                seqs = pp["evidenceSeqs"]
                if not seqs:
                    k["auto_empty_evidence"] += 1
                lo, hi = rng.get(pid, (1, 0))
                k["ev_lines"] += len(seqs); k["ev_in"] += sum(1 for s in seqs if lo <= s <= hi)
            if pred == "UNDECIDED" and pp["decisionText"] == ast.fixed_undecided(point_text[pid]) and not pp["edited"]:
                k["fixed_phrases"] += 1
            row[pid] = {"truth": t["status"], "pred": pred, "strict": ok_strict, "lenient": ok_len}
        for aid, t in c["agendas"].items():
            pred = got.get(aid, {}).get("status")
            k["agenda_cells"] += 1; k["agenda_ok"] += pred == t["status"]
            row[f"agenda:{aid}"] = {"truth": t["status"], "pred": pred, "strict": pred == t["status"]}
        cells[c["id"]] = row
    return {"counts": k, "missedDecisions": missed, "falseDecidedEnd": false_end, "falseDecidedCells": false_cells, "perCheckpoint": cells}


def _rate(a, b):
    return round(a / b, 4) if b else None


def aggregate(scores):
    """회의별 채점 결과를 합쳐 지표와 통과 여부를 낸다."""
    k = {}
    for s in scores.values():
        for key, v in s["counts"].items():
            k[key] = k.get(key, 0) + v
    missed = sum(len(s["missedDecisions"]) for s in scores.values())
    false_end = sum(len(s["falseDecidedEnd"]) for s in scores.values())
    m = {"pointAgreementAll": _rate(k["ok"], k["cells"]), "pointAgreementNonNotStarted": _rate(k["nonns_ok"], k["nonns_cells"]),
         "pointAgreementLenient": _rate(k["ok_lenient"], k["cells"]), "notStartedCells": k["ns_cells"],
         "falseDecided": false_end, "missedDecisions": missed, "agendaAgreement": _rate(k["agenda_ok"], k["agenda_cells"]),
         "evidenceInRange": _rate(k["ev_in"], k["ev_lines"]), "evidenceLines": k["ev_lines"],
         "cells": k["cells"], "agendaCells": k["agenda_cells"]}
    c = CRITERIA
    passed = {"pointAgreementAll": m["pointAgreementAll"] is not None and m["pointAgreementAll"] >= c["pointAgreementAll"],
              "pointAgreementNonNotStarted": m["pointAgreementNonNotStarted"] is not None and m["pointAgreementNonNotStarted"] >= c["pointAgreementNonNotStarted"],
              "maxFalseDecided": false_end <= c["maxFalseDecided"], "maxMissedDecisions": missed <= c["maxMissedDecisions"],
              "agendaAgreement": m["agendaAgreement"] is not None and m["agendaAgreement"] >= c["agendaAgreement"],
              "evidenceInRange": m["evidenceInRange"] is None or m["evidenceInRange"] >= c["evidenceInRange"]}
    ref = {"falseDecidedCellsAllCheckpoints": [x for s in scores.values() for x in s["falseDecidedCells"]],
           "fixedPhrasePoints": k["fixed_phrases"]}
    auto = {"missingPointsInOutput": k["auto_missing_point"], "decidedOrCheckWithEmptyEvidence": k["auto_empty_evidence"]}
    return {"metrics": m, "pass": passed, "reference": ref, "autoChecks": auto, "autoChecksOk": not any(auto.values())}


# --------------------------------------------------------------------------------------
# 지어낸 문장 채점표 (M7, 사람이 채점)
# --------------------------------------------------------------------------------------
def build_sheet(name, snaps, truth, point_text, lines_by_id, seen):
    """AI가 쓴 문장(결정, 후보, 미결. 고정 문구와 사람이 고친 글은 제외)을 근거 줄과 나란히 적은 항목 목록. 같은 문장은 한 번만."""
    out = []
    agenda_of = {p["id"]: p["agendaId"] for p in truth["points"]}
    order = [c["id"] for c in truth["checkpoints"]] + ["END"]
    for cid in order:
        for aid, g in snaps[cid].items():
            for pp in g.get("points", []):
                pid = pp["decisionPointId"]
                if pp["status"] not in ("DECIDED", "DECISION_CHECK", "UNDECIDED") or pp["edited"]:
                    continue
                if pp["status"] == "UNDECIDED" and pp["decisionText"] == ast.fixed_undecided(point_text[pid]):
                    continue
                key = (name, pid, pp["decisionText"], tuple(pp["evidenceSeqs"]))
                if key in seen:
                    continue
                seen.add(key)
                ev = [f"    [{s}] {mmss(lines_by_id[s]['start_ms'])} {lines_by_id[s]['text']}" for s in pp["evidenceSeqs"] if s in lines_by_id]
                out.append(f"## {name} | {cid} | {aid}/{pid} | 정답 {truth_status(truth, cid, pid)} | AI {pp['status']}\n"
                           f"사항: {point_text[pid]}\n문장: {pp['decisionText']}\n근거 줄 ({len(pp['evidenceSeqs'])}개):\n" + ("\n".join(ev) if ev else "    (없음)") +
                           "\n채점: [ ] 근거 줄에 있는 내용만 쓴 문장   [ ] 근거 줄에 없는 내용이 있음 (무엇: ________)\n")
    return out


def truth_status(truth, cid, pid):
    c = truth["end"] if cid == "END" else next(x for x in truth["checkpoints"] if x["id"] == cid)
    return c["points"][pid]["status"]


# --------------------------------------------------------------------------------------
def selftest():
    # 정답 하나: 안건 a1(p1 결정, p2 미결, p3 논의 전), 체크포인트 C1 과 종료
    def cell(st, rng=None, also=()):
        e = {"status": st, "alsoOk": list(also)}
        if rng:
            e["evidenceSeqs"] = list(range(rng[0], rng[1] + 1)); e["evidenceRange"] = list(rng)
        return e
    truth = {"points": [{"id": "p1", "agendaId": "a1", "text": "가"}, {"id": "p2", "agendaId": "a1", "text": "나"}, {"id": "p3", "agendaId": "a1", "text": "다"}],
             "checkpoints": [{"id": "C1", "atMs": 1, "points": {"p1": cell("DECIDED", (3, 5)), "p2": cell("UNDECIDED"), "p3": cell("NOT_STARTED")},
                              "agendas": {"a1": {"status": "UNDECIDED"}}}],
             "end": {"id": "END", "points": {"p1": cell("DECIDED", (3, 5)), "p2": cell("UNDECIDED"), "p3": cell("NOT_STARTED", also=("UNDECIDED",))},
                     "agendas": {"a1": {"status": "UNDECIDED"}}}}
    text = {"p1": "가", "p2": "나", "p3": "다"}

    def pt(pid, st, ev=(), txt="문장", edited=False):
        return {"decisionPointId": pid, "status": st, "decisionText": txt, "evidenceSeqs": list(ev), "edited": edited, "needsCheck": False}

    def snap(p1="DECIDED", p2="UNDECIDED", p3="NOT_STARTED", ev=(3, 4), agenda="UNDECIDED"):
        a = {"a1": {"status": agenda, "reason": None, "points": [pt("p1", p1, ev), pt("p2", p2, (6,)), pt("p3", p3)]}}
        return {"C1": a, "END": copy.deepcopy(a)}

    s = score_meeting(snap(), truth, text)
    agg = aggregate({"X": s})
    assert agg["metrics"]["pointAgreementAll"] == 1.0 and agg["metrics"]["agendaAgreement"] == 1.0 and agg["metrics"]["evidenceInRange"] == 1.0
    assert agg["metrics"]["cells"] == 6 and agg["metrics"]["notStartedCells"] == 2 and agg["autoChecksOk"], agg
    assert all(agg["pass"].values()), agg["pass"]
    # 종료 때 정답이 미결인 p2 를 결정으로 내면 잘못된 결정 1, 체크포인트에도 셈
    s = score_meeting(snap(p2="DECIDED"), truth, text)
    assert s["falseDecidedEnd"] == ["p2"] and s["falseDecidedCells"] == ["C1:p2", "END:p2"]
    agg = aggregate({"X": s})
    assert agg["metrics"]["falseDecided"] == 1 and not agg["pass"]["maxFalseDecided"] and agg["metrics"]["pointAgreementAll"] == round(4 / 6, 4)
    # 결정 누락: p1 을 결정 확인 필요로 낸다 (종료 칸 1)
    s = score_meeting(snap(p1="DECISION_CHECK"), truth, text)
    assert s["missedDecisions"] == ["p1"] and s["falseDecidedEnd"] == []
    # alsoOk: p3 을 미결로 내면 체크포인트(정답 논의 전, alsoOk 없음)와 종료 모두 엄격은 틀린다. 종료는 alsoOk 에 미결이 있어 너그러운 기준은 맞다
    s = score_meeting(snap(p3="UNDECIDED"), truth, text)
    assert s["counts"]["ok"] == 4 and s["counts"]["ok_lenient"] == 5 and s["counts"]["ns_ok"] == 0, s["counts"]
    # 근거가 정답 범위 밖이면 M6 이 내려간다: 3~5 중 [3, 9] -> 1/2 (두 칸 합쳐 4줄 중 2줄)
    s = score_meeting(snap(ev=(3, 9)), truth, text)
    assert aggregate({"X": s})["metrics"]["evidenceInRange"] == 0.5 and not aggregate({"X": s})["pass"]["evidenceInRange"]
    # 안건 상태 불일치: 5 칸 중... 체크포인트와 종료 2칸 모두 DECIDED 로 내면 일치율 0
    s = score_meeting(snap(agenda="DECIDED"), truth, text)
    assert aggregate({"X": s})["metrics"]["agendaAgreement"] == 0.0
    # 자동 점검: 결정인데 근거가 비면 걸린다. 사항이 출력에 없으면 걸린다
    s = score_meeting(snap(ev=()), truth, text)
    assert s["counts"]["auto_empty_evidence"] == 2 and not aggregate({"X": s})["autoChecksOk"]
    broken = snap()
    broken["END"] = {"a1": {"status": "UNDECIDED", "reason": None, "points": [pt("p1", "DECIDED", (3,))]}}
    s = score_meeting(broken, truth, text)
    assert s["counts"]["auto_missing_point"] == 2 and not aggregate({"X": s})["autoChecksOk"]
    # 고정 문구(미결)는 세고, 채점표에서는 빼고, 사람이 고친 글도 뺀다
    fixed = snap()
    fixed["END"]["a1"]["points"][1] = pt("p2", "UNDECIDED", (), ast.fixed_undecided("나"))
    fixed["END"]["a1"]["points"][0] = pt("p1", "DECIDED", (3,), "사람이 고침", edited=True)
    s = score_meeting(fixed, truth, text)
    assert s["counts"]["fixed_phrases"] == 1
    sheet = build_sheet("X", fixed, truth, text, {i: {"start_ms": i * 1000, "text": f"줄{i}"} for i in range(1, 12)}, set())
    assert not any("사람이 고침" in x or "아직 결정되지 않았어요" in x for x in sheet) and len(sheet) >= 1
    again = set()
    n1 = len(build_sheet("X", snap(), truth, text, {3: {"start_ms": 3000, "text": "줄3"}, 4: {"start_ms": 4000, "text": "줄4"}, 6: {"start_ms": 6000, "text": "줄6"}}, again))
    n2 = len(build_sheet("X", snap(), truth, text, {}, again))
    assert n1 == 2 and n2 == 0, (n1, n2)       # 같은 문장은 한 번만 (p1 결정, p2 미결)
    # 정답이 결정 확인 필요인 칸을 결정으로 내면 잘못된 결정. 정답의 alsoOk 에 결정이 있으면 잘못된 결정이 아니다
    t2 = copy.deepcopy(truth)
    t2["checkpoints"][0]["points"]["p2"] = cell("DECISION_CHECK")
    t2["end"]["points"]["p2"] = cell("DECISION_CHECK")
    s = score_meeting(snap(p2="DECIDED"), t2, text)
    assert s["falseDecidedEnd"] == ["p2"] and s["falseDecidedCells"] == ["C1:p2", "END:p2"], s
    t2["end"]["points"]["p2"] = cell("DECISION_CHECK", also=("DECIDED",))
    s = score_meeting(snap(p2="DECIDED"), t2, text)
    assert s["falseDecidedEnd"] == [] and s["falseDecidedCells"] == ["C1:p2"], s
    # 한 사항의 정답 근거 범위는 여러 칸의 범위를 합친 것이다 (C1 3~5, 종료 7~9 -> 3~9)
    t3 = copy.deepcopy(truth)
    t3["end"]["points"]["p1"] = cell("DECIDED", (7, 9))
    s = score_meeting(snap(ev=(6, 8)), t3, text)
    assert aggregate({"X": s})["metrics"]["evidenceInRange"] == 1.0 and aggregate({"X": s})["metrics"]["evidenceLines"] == 4
    # 결정 누락은 2개 이하면 통과, 3개면 통과하지 못한다
    many = {"points": [{"id": f"q{i}", "agendaId": "a1", "text": "x"} for i in range(3)], "checkpoints": [],
            "end": {"id": "END", "points": {f"q{i}": cell("DECIDED", (1, 2)) for i in range(3)}, "agendas": {"a1": {"status": "DECIDED"}}}}
    qtext = {f"q{i}": "x" for i in range(3)}
    for n_ok, want in ((0, False), (1, True), (3, True)):
        a = {"a1": {"status": "DECIDED", "reason": None, "points": [pt(f"q{i}", "DECIDED" if i < n_ok else "UNDECIDED", (1,)) for i in range(3)]}}
        agg = aggregate({"X": score_meeting({"END": a}, many, qtext)})
        assert agg["metrics"]["missedDecisions"] == 3 - n_ok and agg["pass"]["maxMissedDecisions"] is want, (n_ok, agg["metrics"])
    # 기준 상수가 설계서와 같다
    assert CRITERIA == {"pointAgreementAll": 0.80, "pointAgreementNonNotStarted": 0.70, "maxFalseDecided": 0, "maxMissedDecisions": 2,
                        "agendaAgreement": 0.85, "evidenceInRange": 0.90, "fabricatedSentences": 0}
    print("eval_decision_points selftest OK")


def parse_meeting(spec):
    parts = [x.strip() for x in spec.split(",")]
    if len(parts) != 5:
        sys.exit(f"--meeting 은 이름,전사,안건 목록,라벨,정답 사항 파일 다섯 개를 쉼표로 이어야 합니다: {spec}")
    return dict(zip(("name", "transcript", "agendas", "labels", "truth"), parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meeting", action="append", help="이름,전사.jsonl,안건 목록.json,라벨.tsv,정답 사항.json (여러 번)")
    ap.add_argument("--provider", default="echo")
    ap.add_argument("--model")
    ap.add_argument("--dry-run", action="store_true", help="모델을 부르지 않고 호출 수와 비용 추정만 낸다")
    ap.add_argument("--paid-ok", action="store_true", help="유료 호출 허용 (기준과 비용을 확인한 뒤에만)")
    ap.add_argument("--min-new-lines", type=int, help="새 줄이 이만큼 쌓이면 바로 판정 (기본은 서비스 설정). 비용을 줄이는 평가용 설정이며 결과 JSON에 기록된다")
    ap.add_argument("--use-draft", action="store_true", help="확정 전 정답·라벨 초안을 쓴다 (점검용)")
    ap.add_argument("--out", help="결과 JSON 경로 (유료 실행은 이미 있으면 거부)")
    ap.add_argument("--texts-out", help="지어낸 문장 채점표(M7) 경로. 전사 내용이 들어가므로 data/ 아래에 둔다")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    if not a.meeting:
        sys.exit("--meeting 이 필요합니다")
    eas.SHOW_UNCERTAIN = False
    eas.MIN_NEW_LINES = a.min_new_lines
    paid = a.provider != "echo" and not a.dry_run
    if paid and not a.paid_ok:
        sys.exit("유료 호출입니다. 기준과 비용을 확인한 뒤 --paid-ok 를 붙이세요. (--dry-run 으로 추정만 볼 수 있습니다)")
    if paid and not a.model:
        sys.exit("--model 이 필요합니다")
    if paid:
        eas.load_dotenv(os.path.join(HERE, "..", ".env"))
        if a.provider == "gemini" and not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            sys.exit("GEMINI_API_KEY 를 찾지 못했습니다 (환경변수와 저장소 루트 .env 모두 비어 있음). 호출하지 않고 끝냅니다")
    if paid and (not a.out or os.path.exists(a.out)):
        sys.exit("유료 실행은 --out 이 필요하고, 그 파일이 이미 있으면 다시 돌리지 않습니다 (한 번만)")
    dry = a.dry_run or a.provider == "echo"
    result = {"provider": a.provider, "model": a.model, "dryRun": dry, "minNewLines": a.min_new_lines or ast.StatusCfg().min_new_lines,
              "criteria": CRITERIA, "meetings": {}}
    scores, sheet, seen, total, confirmed_all, invalid = {}, [], set(), {"usd": 0.0, "krw": 0, "calls": 0}, True, False
    for spec in a.meeting:
        m = parse_meeting(spec)
        truth, confirmed = load_truth(m["truth"], a.use_draft)
        confirmed_all &= confirmed
        lines = load_jsonl_as_protocol_lines(m["transcript"])
        batches = make_batches(lines, period_ms=10000)
        with open(m["agendas"], encoding="utf-8") as f:
            agendas_msg = json.load(f)
        gold, _unsure = ea.load_labels(m["labels"], a.use_draft)
        point_text = {p["id"]: p["text"] for p in truth["points"]}
        t0 = time.time()
        snaps, cls, judge, crec, srec = eas.replay(batches, agendas_msg, gold, "gold", a.provider, a.model, dry, replay_points(truth))
        n_ag = len(agendas_msg["agendas"])
        avg_pts = len(truth["points"]) / max(1, n_ag)
        st_cost = es.cost(srec.in_chars, srec.calls * (OUT_TOK_BASE + OUT_TOK_PER_POINT * avg_pts), a.model)
        r = {"statusCalls": srec.calls, "statusCost": st_cost, "statusInfo": judge.info(), "seconds": round(time.time() - t0, 1)}
        if judge.halted and not dry:                        # 호출이 연속 실패로 멈췄으면 점수를 내지 않는다 (규칙 판정만 남아 수치가 틀려진다)
            r["invalid"] = f"모델 호출이 멈춰 점수를 내지 않음: {judge.halted}"
            invalid = True
        elif not a.dry_run:
            sc = score_meeting(snaps, truth, point_text)
            scores[m["name"]] = sc
            r["score"] = {k: v for k, v in sc.items() if k != "counts"}
            r["counts"] = sc["counts"]
            sheet += build_sheet(m["name"], snaps, truth, point_text, {ln["id"]: ln for ln in lines}, seen)
        result["meetings"][m["name"]] = r
        total["usd"] += st_cost["usd"]; total["krw"] += st_cost["krw"]; total["calls"] += srec.calls
    result["truthConfirmed"] = confirmed_all
    if scores and not invalid:
        result["aggregate"] = aggregate(scores)
        result["aggregate"]["manual"] = {"fabricatedSentences": None, "sentencesToGrade": len(sheet), "criterion": CRITERIA["fabricatedSentences"]}
    result["totalEstimate"] = {"calls": total["calls"], "usd": round(total["usd"], 4), "krw": total["krw"]}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    if a.texts_out and sheet and not invalid:
        os.makedirs(os.path.dirname(os.path.abspath(a.texts_out)), exist_ok=True)
        with open(a.texts_out, "w", encoding="utf-8") as f:
            f.write("# 지어낸 문장 채점표 (M7). 문장에 있는 사실, 이름, 숫자가 근거 줄 안에 있는지만 본다. 근거 줄에 없는 내용이 하나라도 있으면 지어낸 문장이다.\n"
                    "# 이 파일에는 전사 내용이 들어 있으므로 커밋하지 않는다.\n\n" + "\n".join(sheet))
    print(text)
    if invalid:
        print("\n[경고] 모델 호출이 멈춘 회의가 있어 결과가 유효하지 않습니다. 원인을 고친 뒤 새 파일 이름으로 다시 돌려야 합니다.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
