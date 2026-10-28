#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-06 새 안건 제안 시간 경계 평가 (모델 호출 없음, 비용 0원)

구간 길이를 초 단위로 맞춘 합성 대본(해당 없음 구간의 정답이 *_offtopic_runs.json 에 있다)을 실제 서비스처럼 10초 묶음으로 흘려 보내며,
사람이 확정한 분류(labels_confirmed.tsv)를 그대로 써서 제안(SUGGEST)이 언제 몇 번 나오는지를 정답 구간과 맞춰 본다. 제목 모델은 부르지 않는다.
설계와 기준: 프로젝트 문서 claude/ai06-60s-boundary-eval-design-v1.md (기준은 실행 전에 확정, 결과를 본 뒤 바꾸지 않는다).

재는 것
  B1 오경보: 제안이 없어야 하는 구간(또는 어느 구간에도 속하지 않는 자리)에서 제안이 뜬 수       기준 0
  B2 놓침: 제안이 나와야 하는 구간에서 제안이 안 뜬 수                                          기준 0
  B3 중복: 한 구간에서 제안이 2번 이상 뜬 구간 수                                              기준 0
  B4 감지 시각: 제안 시각이 기대 시각과 다른 제안 수                                            기준 0
     기대 시각 = 구간이 임계 시간에 닿는 첫 줄(맞장구·간투사·낮은 확신도 줄은 건너뜀)이 처음 담긴 묶음의 시각 (구간 시작 + 임계 시간보다 한 묶음 안쪽으로 늦다)
  B5 --special 로 준 대본(끼어듦·맞장구가 있는 대본)의 모든 구간이 정답과 같다                     기준 참
자동 점검: 정답 구간 파일의 expectSuggest 가 임계 시간 기준과 같다
결과 JSON 에는 전사를 넣지 않는다 (구간 번호, 길이, 시각, 개수만).

사용
  python3 eval_offtopic_boundary.py --meeting 이름,전사.jsonl,안건 목록.json,라벨.tsv,정답 구간.json [--meeting ...] --special ot60_07,ot60_08 --out R.json
  python3 eval_offtopic_boundary.py --selftest
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from meeting_summary import load_jsonl_as_protocol_lines, make_batches  # noqa: E402
import agenda_offtopic as ao  # noqa: E402
import eval_agenda as ea  # noqa: E402
import eval_agenda_offtopic as eo  # noqa: E402

CRITERIA = {                         # 설계서 3장의 기준. 실행 전에 확정했고 결과를 본 뒤 바꾸지 않는다
    "falseAlarms": 0,                # B1
    "misses": 0,                     # B2
    "duplicates": 0,                 # B3
    "detectionTimeMismatch": 0,      # B4
    "specialAllMatch": True,         # B5
}
BATCH_MS = 10000
SKIP_KINDS = ("filler", "backchannel")


def is_skipped(ln):
    """맞장구, 간투사, 낮은 확신도 줄은 구간을 끊지도 늘리지도 않는다."""
    return ln.get("kind") in SKIP_KINDS or bool(ln.get("low_confidence"))


def expected_detection(run, lines_by_id, first_batch_ms, thr_ms):
    """정답 구간이 임계 시간에 닿았을 때 제안이 처음 나와야 하는 시각(ms). 닿지 못하면 None."""
    start_ms = round(run["startSec"] * 1000)
    for sid in range(run["firstSeq"], run["lastSeq"] + 1):
        ln = lines_by_id[sid]
        if is_skipped(ln):
            continue
        if ln["end_ms"] - start_ms >= thr_ms:
            return first_batch_ms[sid]
    return None


def match_episodes(runs, episodes, slack_ms=BATCH_MS):
    """제안(에피소드)을 시각으로 정답 구간에 붙인다. 구간 시작 <= 시각 <= 구간 끝 + 한 묶음. 어느 구간에도 안 붙으면 unmatched."""
    per, unmatched = [[] for _ in runs], []
    for e in episodes:
        t, k = e["detectedAtMs"], None
        for i, r in enumerate(runs):
            s = round(r["startSec"] * 1000)
            if s <= t <= s + round(r["lengthSec"] * 1000) + slack_ms:
                k = i                                # 여러 구간에 걸치면 더 늦게 시작한 구간
        if k is None:
            unmatched.append(e)
        else:
            per[k].append(e)
    return per, unmatched


def score_meeting(runs, episodes, expected):
    per, unmatched = match_episodes(runs, episodes)
    rows, false_alarms, misses, dups, mism = [], len(unmatched), 0, 0, 0
    for i, r in enumerate(runs):
        eps = per[i]
        row = {"run": i + 1, "firstSeq": r["firstSeq"], "lastSeq": r["lastSeq"], "lengthSec": r["lengthSec"], "expectSuggest": r["expectSuggest"],
               "expectedMs": expected[i], "actualMs": [e["detectedAtMs"] for e in eps]}
        ok = True
        if not r["expectSuggest"]:
            if eps:
                false_alarms += len(eps); ok = False
        else:
            if not eps:
                misses += 1; ok = False
            elif eps[0]["detectedAtMs"] != expected[i]:
                mism += 1; ok = False
        if len(eps) >= 2:
            dups += 1; ok = False
        row["ok"] = ok
        rows.append(row)
    all_match = false_alarms == 0 and misses == 0 and dups == 0 and mism == 0
    return {"falseAlarms": false_alarms, "misses": misses, "duplicates": dups, "detectionTimeMismatch": mism, "allMatch": all_match,
            "unmatchedEpisodes": len(unmatched), "runs": rows}


def aggregate(scores, special, auto_bad):
    k = {key: sum(s[key] for s in scores.values()) for key in ("falseAlarms", "misses", "duplicates", "detectionTimeMismatch")}
    spec = {n: scores[n]["allMatch"] for n in special if n in scores}
    c = CRITERIA
    passed = {"falseAlarms": k["falseAlarms"] <= c["falseAlarms"], "misses": k["misses"] <= c["misses"],
              "duplicates": k["duplicates"] <= c["duplicates"], "detectionTimeMismatch": k["detectionTimeMismatch"] <= c["detectionTimeMismatch"]}
    if special:
        passed["specialAllMatch"] = bool(spec) and all(spec.values()) and len(spec) == len(special)
    runs = [r for s in scores.values() for r in s["runs"]]
    return {"counts": k, "pass": passed, "special": spec, "autoChecksBad": auto_bad, "autoChecksOk": not auto_bad,
            "runs": len(runs), "expectedSuggestions": sum(1 for r in runs if r["expectSuggest"]), "expectedNone": sum(1 for r in runs if not r["expectSuggest"])}


def selftest():
    # 줄 8개: 구간 시작 0, 줄 i 의 끝은 i*10000-1000 (9000, 19000, ... 79000). 묶음은 도착(끝) 시각을 10초 단위로 올려 담는다
    lines = {i: {"id": i, "start_ms": (i - 1) * 10000, "end_ms": i * 10000 - 1000, "kind": None, "low_confidence": False} for i in range(1, 9)}
    first = {i: ((lines[i]["end_ms"] + BATCH_MS - 1) // BATCH_MS) * BATCH_MS for i in lines}
    run = {"firstSeq": 1, "lastSeq": 8, "startSec": 0.0, "lengthSec": 79.0, "expectSuggest": True}
    assert expected_detection(run, lines, first, 60000) == 70000          # 줄 7 의 끝 69000 에서 60초를 넘고, 그 줄이 담긴 묶음은 70000
    assert expected_detection(run, lines, first, 59000) == 60000          # 줄 6 의 끝 59000 에서 닿는다 (경계 이상). 59000 은 60000 묶음에 담긴다
    assert expected_detection(run, lines, first, 79001) is None           # 끝까지 닿지 못한다
    lines[7] = dict(lines[7], kind="backchannel")                          # 60초를 넘는 줄이 맞장구면 건너뛰고 다음 줄 8 (끝 79000 -> 묶음 80000)
    assert expected_detection(run, lines, first, 60000) == 80000
    lines[7] = dict(lines[7], kind="filler")
    assert expected_detection(run, lines, first, 60000) == 80000
    lines[7] = dict(lines[7], kind=None, low_confidence=True)
    assert expected_detection(run, lines, first, 60000) == 80000
    # 에피소드 붙이기와 채점
    runs = [{"firstSeq": 1, "lastSeq": 5, "startSec": 0.0, "lengthSec": 50.0, "expectSuggest": False},
            {"firstSeq": 8, "lastSeq": 20, "startSec": 100.0, "lengthSec": 80.0, "expectSuggest": True}]
    exp = [None, 170000]
    ep = lambda t: {"detectedAtMs": t}
    s = score_meeting(runs, [ep(170000)], exp)
    assert (s["falseAlarms"], s["misses"], s["duplicates"], s["detectionTimeMismatch"], s["allMatch"]) == (0, 0, 0, 0, True), s
    s = score_meeting(runs, [ep(60000), ep(170000)], exp)                  # 구간 1(제안 없음)에 시각이 붙은 제안은 아니다: 60000 은 구간 끝 50000 + 한 묶음 안
    assert s["falseAlarms"] == 1 and not s["allMatch"], s
    s = score_meeting(runs, [ep(500000), ep(170000)], exp)                 # 어느 구간에도 안 붙는 제안도 오경보다
    assert s["falseAlarms"] == 1 and s["unmatchedEpisodes"] == 1, s
    s = score_meeting(runs, [], exp)
    assert s["misses"] == 1 and s["falseAlarms"] == 0 and not s["allMatch"], s
    s = score_meeting(runs, [ep(170000), ep(175000)], exp)
    assert s["duplicates"] == 1 and s["detectionTimeMismatch"] == 0, s
    s = score_meeting(runs, [ep(160000)], exp)                             # 시각이 기대와 다르다
    assert s["detectionTimeMismatch"] == 1 and s["misses"] == 0 and not s["allMatch"], s
    s = score_meeting(runs, [ep(180000)], exp)                             # 구간 끝(180000)에 정확히 닿은 시각도 구간에 붙는다
    assert s["detectionTimeMismatch"] == 1 and s["unmatchedEpisodes"] == 0, s
    s = score_meeting(runs, [ep(190000)], exp)                             # 구간 끝 + 한 묶음(190000)까지 붙는다
    assert s["unmatchedEpisodes"] == 0 and s["detectionTimeMismatch"] == 1
    s = score_meeting(runs, [ep(190001)], exp)
    assert s["unmatchedEpisodes"] == 1 and s["misses"] == 1
    # 합치기와 통과 판정
    good = score_meeting(runs, [ep(170000)], exp)
    bad = score_meeting(runs, [ep(60000), ep(170000)], exp)
    agg = aggregate({"A": good, "B": bad}, ["A", "B"], 0)
    assert agg["counts"]["falseAlarms"] == 1 and not agg["pass"]["falseAlarms"] and agg["pass"]["misses"] and agg["special"] == {"A": True, "B": False}
    assert not agg["pass"]["specialAllMatch"]
    assert aggregate({"A": good}, ["A"], 0)["pass"]["specialAllMatch"] is True
    assert aggregate({"A": good}, ["A", "Z"], 0)["pass"]["specialAllMatch"] is False      # 없는 대본을 특수로 줬으면 통과가 아니다
    assert "specialAllMatch" not in aggregate({"A": good}, [], 0)["pass"]
    assert aggregate({"A": good}, [], 2)["autoChecksOk"] is False
    assert aggregate({"A": good, "B": bad}, [], 0)["expectedSuggestions"] == 2 and aggregate({"A": good, "B": bad}, [], 0)["expectedNone"] == 2
    # 붙이기 경계와 겹침: 구간 시작 시각에 정확히 뜬 제안은 그 구간에 붙고, 두 구간의 창이 겹치면 더 늦게 시작한 구간에 붙는다
    s = score_meeting(runs, [ep(100000)], exp)
    assert s["unmatchedEpisodes"] == 0 and s["runs"][1]["actualMs"] == [100000], s
    near = [{"firstSeq": 1, "lastSeq": 5, "startSec": 0.0, "lengthSec": 50.0, "expectSuggest": False},
            {"firstSeq": 8, "lastSeq": 20, "startSec": 55.0, "lengthSec": 80.0, "expectSuggest": True}]
    per, un = match_episodes(near, [ep(58000)])
    assert per[0] == [] and per[1] == [ep(58000)] and un == []
    # 오경보는 제안 개수만큼 센다 (한 구간에서 두 번 떠도 둘 다)
    s = score_meeting(runs, [ep(20000), ep(30000), ep(170000)], exp)
    assert s["falseAlarms"] == 2 and s["duplicates"] == 1, s              # 한 구간에서 2번 이상 뜬 구간이므로 중복도 1
    # 중복만 있는 경우도 allMatch 가 아니다
    s = score_meeting(runs, [ep(170000), ep(175000)], exp)
    assert not s["allMatch"], s
    # 통과 판정: 놓침, 중복, 시각 불일치 각각이 기준을 어긴다
    for eps_, key in (([], "misses"), ([ep(170000), ep(175000)], "duplicates"), ([ep(160000)], "detectionTimeMismatch")):
        a2 = aggregate({"A": score_meeting(runs, eps_, exp)}, [], 0)
        assert a2["pass"][key] is False, (key, a2["pass"])
    # 제안 기대/없음 기대 개수는 구간에서 센다 (기대 2, 없음 1)
    runs3 = runs + [{"firstSeq": 30, "lastSeq": 40, "startSec": 300.0, "lengthSec": 80.0, "expectSuggest": True}]
    a3 = aggregate({"A": score_meeting(runs3, [ep(170000), ep(370000)], [None, 170000, 370000])}, [], 0)
    assert (a3["expectedSuggestions"], a3["expectedNone"], a3["runs"]) == (2, 1, 3), a3
    assert CRITERIA == {"falseAlarms": 0, "misses": 0, "duplicates": 0, "detectionTimeMismatch": 0, "specialAllMatch": True}
    print("eval_offtopic_boundary selftest OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meeting", action="append", help="이름,전사.jsonl,안건 목록.json,라벨.tsv,정답 구간.json (여러 번)")
    ap.add_argument("--threshold-sec", type=int, default=ao.OffTopicCfg().threshold_sec, help="제안 시간(초). 기본은 서비스 기본값(60초)")
    ap.add_argument("--special", default="", help="끼어듦·맞장구가 있는 대본 이름(쉼표로). 이 대본들의 모든 구간이 정답과 같아야 한다(B5)")
    ap.add_argument("--use-draft", action="store_true", help="확정 전 라벨 초안을 쓴다 (점검용)")
    ap.add_argument("--out", help="결과 JSON 경로")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    if not a.meeting:
        sys.exit("--meeting 이 필요합니다")
    thr_ms = a.threshold_sec * 1000
    scores, auto_bad, labels_draft = {}, 0, bool(a.use_draft)
    for spec in a.meeting:
        parts = [x.strip() for x in spec.split(",")]
        if len(parts) != 5:
            sys.exit(f"--meeting 은 이름,전사,안건 목록,라벨,정답 구간 다섯 개를 쉼표로 이어야 합니다: {spec}")
        name, transcript, agendas, labels, runs_path = parts
        lines = load_jsonl_as_protocol_lines(transcript)
        batches = make_batches(lines, period_ms=BATCH_MS)
        with open(agendas, encoding="utf-8") as f:
            agendas_msg = json.load(f)
        with open(runs_path, encoding="utf-8") as f:
            runs = json.load(f)["runs"]
        gold, _unsure = ea.load_labels(labels, a.use_draft)
        res, _rec, _screen = eo.replay(batches, agendas_msg, gold, "echo", None, True, False, {"threshold_sec": a.threshold_sec})
        lines_by_id = {ln["id"]: ln for ln in lines}
        first_batch = {}
        for b in batches:
            for ln in b["lines"]:
                first_batch.setdefault(ln["id"], b["sent_ms"])
        expected = [expected_detection(r, lines_by_id, first_batch, thr_ms) for r in runs]
        auto_bad += sum(1 for r, e in zip(runs, expected) if (e is not None) != bool(r["expectSuggest"]))   # 정답 파일의 기대와 임계 시간 기준이 다르면 걸린다
        scores[name] = score_meeting(runs, res["episodes"], expected)
        scores[name]["maxNoneRunSec"] = res["maxNoneRunSec"]
    special = [x for x in a.special.split(",") if x]
    out = {"thresholdSec": a.threshold_sec, "labelsAreDraft": labels_draft, "criteria": CRITERIA, "meetings": scores,
           "aggregate": aggregate(scores, special, auto_bad), "cost": "0원 (모델 호출 없음)"}
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)


if __name__ == "__main__":
    main()
