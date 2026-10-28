#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
안건별 요약 평가 (AI-03)

끝난 회의 전사를 실제 서비스처럼 10초 묶음으로 흘려 보내 안건별 요약기를 돌리고, 사람이 확정한 정답과 맞춰 본다.
분류 입력을 두 가지로 돌려 요약이 분류 오류를 얼마나 물려받는지 잰다.
  gold : 사람이 확정한 분류(labels_confirmed.tsv)를 그대로 쓴다. 요약기 자체의 품질만 잰다. 분류 모델은 부르지 않는다.
  ai   : AI-02 분류기가 낸 분류를 쓴다. 분류 호출 비용이 따로 든다.

자동으로 재는 것 (정답 전사·라벨만 있으면 됨)
  - 근거 정확도: 문장의 근거 줄 중 그 안건의 정답 줄인 비율 (gold 기준)
  - 핵심 내용 포함(근거 겹침 근사): 핵심 내용의 정답 근거 줄이 한 문장의 근거와 겹치면 포함으로 센다
  - 안건당 문장 수, 검증기 경고가 붙은 문장 수, 호출 수, 비용 추정
사람이 채점표(--grading-out)로 확인하는 것: 문장이 근거 줄로 뒷받침되는가, 사실 오류(없는 사실·틀린 숫자)가 있는가, 핵심 내용이 정말 담겼는가.

사용
  # 모델 호출 없이 호출 수와 비용 추정만 (기본, 비용 0)
  python3 eval_agenda_summary.py --transcript T.jsonl --agendas A.json --labels L.tsv --keypoints K.json --dry-run --provider gemini --model gemini-3.8-flash
  # 에코 모의 요약기로 스크립트 점검 (비용 0)
  python3 eval_agenda_summary.py ... --provider echo
  # 유료 평가 (한 번만. 결과 파일이 이미 있으면 거부) -- 키는 환경변수로만
  python3 eval_agenda_summary.py ... --provider gemini --model gemini-3.8-flash --paid-ok --out R.json --grading-out G.tsv

핵심 내용 파일(JSON)은 사람이 확정해야 한다. confirmed 가 true 가 아니면 --use-draft 일 때만 쓴다 (점검용, 최종 수치 아님).
결과 JSON 에는 전사와 요약 문장을 넣지 않는다 (줄 번호와 문장 번호만). 채점표(TSV)에는 사람이 읽어야 하므로 내용이 들어 있다.
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from meeting_summary import load_jsonl_as_protocol_lines, make_batches  # noqa: E402
from meeting_live_summary_v2 import PRICES, USD_KRW_ASSUMED  # noqa: E402
from input_adapter import adapt_agendas  # noqa: E402
import agenda_classifier as ac  # noqa: E402
import agenda_summarizer as asum  # noqa: E402
import eval_agenda as ea  # noqa: E402

CRITERIA = {                       # 제안 기준. 실행 전에 확인받는다
    "evidenceAccuracy": 0.90,      # 근거 줄이 그 안건의 정답 줄인 비율 (gold)
    "keypointCoverage": 0.70,      # 핵심 내용 포함 비율 (근거 겹침 근사, 사람 채점으로 다시 확인)
    "maxSentences": 3,
}
TOK_PER_KO_CHAR = 2                # 비용 추정 가정 (다른 평가와 같음)
OUT_TOK_PER_CALL = 120             # 호출 한 번의 출력 (문장 3개 안팎) 추정
MODES = ("gold", "ai")


class SumRecorder:
    """안건 요약 모델 호출을 감싸 호출 수와 입력 글자 수를 센다. dry 이면 모델을 부르지 않고 에코 답을 쓴다."""

    def __init__(self, inner, dry):
        self.inner, self.dry = inner, dry
        self.calls, self.in_chars = 0, 0

    def __call__(self, user):
        self.calls += 1
        self.in_chars += len(asum.AGENDA_SYSTEM_PROMPT) + len(user)
        return asum.echo_llm(user) if self.dry else self.inner(user)


def load_keypoints(path, use_draft):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not d.get("confirmed") and not use_draft:
        sys.exit("핵심 내용 파일이 아직 확정되지 않았습니다 (confirmed 가 true 가 아님). 사람이 확인한 뒤 true 로 바꾸세요. 점검용이면 --use-draft")
    return d["agendas"], bool(d.get("confirmed"))


MIN_NEW_LINES = None          # --min-new-lines: 새 줄이 이만큼 쌓여야 요약한다 (None 이면 서비스 기본값)


def replay(lines, batches, agendas_msg, gold, mode, provider, model, dry):
    ad = adapt_agendas(agendas_msg)
    ccfg = ac.ClassifyCfg(provider=provider, model=model)
    crec = ea.Recorder(ac.make_llm(ccfg), dry)
    cls = ac.AgendaClassifier(ad["meeting_id"], ccfg, llm=crec)
    cls.set_agendas(ad["version"], ad["agendas"])
    if mode == "gold":                                      # 사람이 확정한 분류를 고정하고 분류 모델은 부르지 않는다
        cls.apply_corrections([{"seq": s, "agenda_id": a, "rev": 1} for s, a in gold.items()])
        cls.halted = "평가: 사람이 확정한 분류만 사용"
    scfg = asum.SummarizeCfg(provider=provider, model=model, **({"min_new_lines": MIN_NEW_LINES} if MIN_NEW_LINES else {}))
    srec = SumRecorder(asum.make_llm(scfg), dry)
    summ = asum.AgendaSummarizer(cls, scfg, llm=srec, id_prefix="e")
    for b in batches:
        cls.receive(b)
        cls.advance(b["sent_ms"])
        cls.process()
        summ.process()
    cls.advance(batches[-1]["sent_ms"] + 1000)
    cls.process(final=True)
    summ.process(final=True)
    board = summ.take_board()
    return board, cls, summ, crec, srec


def cost(in_chars, out_tok, model):
    pin, pout = PRICES.get(model, PRICES["gemini-3.8-flash"])
    tin = in_chars * TOK_PER_KO_CHAR
    usd = tin / 1e6 * pin + out_tok / 1e6 * pout
    return {"inTokens": tin, "outTokens": out_tok, "usd": round(usd, 4), "krw": round(usd * USD_KRW_ASSUMED)}


def score(board, gold, keypoints):
    gold_by_agenda = {}
    for s, a in gold.items():
        gold_by_agenda.setdefault(a, set()).add(s)
    out, tot_ev, ok_ev, tot_kp, hit_kp = {}, 0, 0, 0, 0
    for a in board["agendas"]:
        aid = a["agendaId"]
        own = gold_by_agenda.get(aid, set())
        ev = [s for x in a["summary"] for s in x["evidenceSeqs"]]
        good = [s for s in ev if s in own]
        foreign = [x["id"] for x in a["summary"] if any(s not in own for s in x["evidenceSeqs"])]
        kps = keypoints.get(aid, [])
        covered = [k["id"] for k in kps if any(set(k["evidenceSeqs"]) & set(x["evidenceSeqs"]) for x in a["summary"])]
        out[aid] = {"sentences": len(a["summary"]), "evidenceSeqs": len(ev), "evidenceInAgenda": len(good),
                    "sentencesWithForeignEvidence": foreign, "keypoints": len(kps), "keypointsCovered": covered,
                    "keypointsMissed": [k["id"] for k in kps if k["id"] not in covered]}
        tot_ev += len(ev); ok_ev += len(good); tot_kp += len(kps); hit_kp += len(covered)
    return {"perAgenda": out,
            "evidenceAccuracy": round(ok_ev / tot_ev, 4) if tot_ev else None,
            "keypointCoverage": round(hit_kp / tot_kp, 4) if tot_kp else None,
            "maxSentencesPerAgenda": max((len(a["summary"]) for a in board["agendas"]), default=0),
            # 문장이 넓은 구간을 근거로 대면 "근거 겹침" 포함 판정이 후해진다. 사람 채점과 함께 봐야 하는 이유
            "avgEvidencePerSentence": round(tot_ev / max(1, sum(len(a["summary"]) for a in board["agendas"])), 1)}


def judge(sc, mode):
    c = CRITERIA
    r = {"maxSentences": sc["maxSentencesPerAgenda"] <= c["maxSentences"],
         "keypointCoverage": sc["keypointCoverage"] is not None and sc["keypointCoverage"] >= c["keypointCoverage"]}
    if mode == "gold":                                      # ai 모드는 분류가 틀리면 근거도 달라지므로 참고용
        r["evidenceAccuracy"] = sc["evidenceAccuracy"] is not None and sc["evidenceAccuracy"] >= c["evidenceAccuracy"]
    return r


def write_grading(path, results, store, keypoints, agendas_msg):
    """사람이 읽고 채점하는 표. 전사 내용이 들어 있으니 저장소에 올리지 않는다."""
    titles = {a["agendaId"]: a["title"] for a in agendas_msg["agendas"]}
    with open(path, "w", encoding="utf-8") as f:
        f.write("mode\tkind\tagenda\tid\ttext\tevidence\tevidence_text\t지원됨(O/X)\t사실오류(O/X)\t포함됨(O/X)\t메모\n")
        for mode, (board, _cls, _summ) in results.items():
            for a in board["agendas"]:
                for x in a["summary"]:
                    ev = " / ".join(f"[{s}] {store.get(s, '')}" for s in x["evidenceSeqs"])
                    f.write(f"{mode}\t문장\t{titles.get(a['agendaId'], a['agendaId'])}\t{x['id']}\t{x['text']}\t"
                            f"{','.join(map(str, x['evidenceSeqs']))}\t{ev}\t\t\t\t\n")
            for a in board["agendas"]:
                for k in keypoints.get(a["agendaId"], []):
                    cov = [x["id"] for x in a["summary"] if set(k["evidenceSeqs"]) & set(x["evidenceSeqs"])]
                    f.write(f"{mode}\t핵심\t{titles.get(a['agendaId'], a['agendaId'])}\t{k['id']}\t{k['text']}\t"
                            f"{','.join(map(str, k['evidenceSeqs']))}\t겹친 문장: {','.join(cov) or '없음'}\t\t\t\t\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--agendas", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--keypoints", required=True)
    ap.add_argument("--provider", default="echo")
    ap.add_argument("--model")
    ap.add_argument("--modes", default="gold,ai")
    ap.add_argument("--dry-run", action="store_true", help="모델을 부르지 않고 호출 수와 비용 추정만 낸다")
    ap.add_argument("--paid-ok", action="store_true", help="유료 호출 허용 (기준과 비용을 확인한 뒤에만)")
    ap.add_argument("--use-draft", action="store_true", help="확정 전 핵심 내용·라벨 초안을 쓴다 (점검용)")
    ap.add_argument("--min-new-lines", type=int, help="새 줄이 이만큼 쌓이면 바로 요약 (기본은 서비스 설정). 비용을 줄이는 평가용 설정이며 결과 JSON에 기록된다. 기존 결과와 직접 비교하지 않는다")
    ap.add_argument("--out", help="결과 JSON 경로 (유료 실행은 이미 있으면 거부)")
    ap.add_argument("--grading-out", help="사람이 채점할 표(TSV) 경로. 이미 있으면 거부")
    a = ap.parse_args()
    global MIN_NEW_LINES
    MIN_NEW_LINES = a.min_new_lines

    paid = a.provider != "echo" and not a.dry_run
    if paid and not a.paid_ok:
        sys.exit("유료 호출입니다. 기준과 비용을 확인한 뒤 --paid-ok 를 붙이세요. (--dry-run 으로 추정만 볼 수 있습니다)")
    if paid and not a.model:
        sys.exit("--model 이 필요합니다")
    if paid and (not a.out or os.path.exists(a.out)):
        sys.exit("유료 실행은 --out 이 필요하고, 그 파일이 이미 있으면 다시 돌리지 않습니다 (한 번만)")
    if a.grading_out and os.path.exists(a.grading_out):
        sys.exit("--grading-out 파일이 이미 있습니다")
    modes = a.modes.split(",")
    if any(m not in MODES for m in modes):
        sys.exit(f"--modes 는 {', '.join(MODES)} 중에서 고르세요")

    lines = load_jsonl_as_protocol_lines(a.transcript)
    batches = make_batches(lines, period_ms=10000)
    with open(a.agendas, encoding="utf-8") as f:
        agendas_msg = json.load(f)
    gold, unsure = ea.load_labels(a.labels, a.use_draft)
    keypoints, kp_confirmed = load_keypoints(a.keypoints, a.use_draft)
    dry = a.dry_run or a.provider == "echo"
    result = {"provider": a.provider, "model": a.model, "dryRun": dry, "labels": len(gold), "keypointsConfirmed": kp_confirmed,
              "minNewLines": a.min_new_lines or asum.SummarizeCfg().min_new_lines, "criteria": CRITERIA, "modes": {}}
    total = {"usd": 0.0, "krw": 0, "calls": 0}
    graded = {}
    for mode in modes:
        t0 = time.time()
        board, cls, summ, crec, srec = replay(lines, batches, agendas_msg, gold, mode, a.provider, a.model, dry)
        sum_cost = cost(srec.in_chars, srec.calls * OUT_TOK_PER_CALL, a.model)
        cls_cost = ea.estimate_cost(crec, a.model) if crec.calls else None
        r = {"summaryCalls": srec.calls, "summaryCost": sum_cost, "classifyCost": cls_cost,
             "summaryInfo": summ.info(), "seconds": round(time.time() - t0, 1)}
        if not a.dry_run:
            r["score"] = score(board, gold, keypoints)
            r["pass"] = judge(r["score"], mode)
            graded[mode] = (board, cls, summ)
        result["modes"][mode] = r
        total["usd"] += sum_cost["usd"] + (cls_cost["usd"] if cls_cost else 0)
        total["krw"] += sum_cost["krw"] + (cls_cost["krw"] if cls_cost else 0)
        total["calls"] += srec.calls + crec.calls
    result["totalEstimate"] = {"calls": total["calls"], "usd": round(total["usd"], 4), "krw": total["krw"]}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    if a.grading_out and graded:
        store = {ln["id"]: ln["text"] for ln in next(iter(graded.values()))[1].store.snapshot()}
        write_grading(a.grading_out, graded, store, keypoints, adapt_agendas_ids(agendas_msg))
    print(text)


def adapt_agendas_ids(msg):
    return {"agendas": [{"agendaId": x["agendaId"], "title": x["title"]} for x in msg["agendas"]]}


if __name__ == "__main__":
    main()
