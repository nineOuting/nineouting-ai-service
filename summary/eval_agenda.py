#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
안건 분류 평가 (AI-02)

끝난 회의 전사를 실제 서비스처럼 10초 묶음으로 흘려 보내 분류기를 돌리고, 사람이 확정한 라벨과 맞춰 본다.
같은 회의를 변형 두 가지로 돌려 직전 대화의 효과를 잰다.
  with_context     : 기본 설정 (직전 12줄을 함께 보여 줌)
  without_context  : 직전 대화 없이 줄만 보여 줌

사용
  # 모델 호출 없이 호출 수와 비용 추정만 (기본, 비용 0)
  python3 eval_agenda.py --transcript T.jsonl --agendas A.json --labels L.tsv --dry-run --provider gemini --model gemini-3.8-flash
  # 에코 모의 분류기로 스크립트 점검 (비용 0)
  python3 eval_agenda.py ... --provider echo
  # 유료 평가 (한 번만. 결과 파일이 이미 있으면 거부) -- 키는 환경변수로만
  python3 eval_agenda.py ... --provider gemini --model gemini-3.8-flash --paid-ok

라벨 파일(TSV): seq, time, draft, unsure, confirmed, text. 정답은 confirmed 열이다.
  confirmed 가 비어 있으면 --use-draft 일 때만 draft 를 쓴다 (초안 점검용. 최종 수치는 확정 라벨로만 낸다).
  값은 안건 id(예: a1) 또는 빈칸(해당 없음). 확정했는데 해당 없음이면 "-" 로 적는다.
출력에 전사 내용은 넣지 않는다 (줄 번호와 안건 id만).
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

PASS_ACCURACY = 0.80          # PRD 목표 (베타에서 확정)
VARIANTS = {
    "with_context": dict(),
    "without_context": dict(context_lines=0),
}
TOK_PER_KO_CHAR = 2           # 비용 추정 가정 (v2 평가와 같음)
OUT_TOK_PER_LINE = 10
OUT_TOK_BASE = 15


def load_labels(path, use_draft):
    """seq -> 안건 id 또는 None. 정답이 없는 줄은 들어 있지 않다."""
    gold, unsure = {}, set()
    with open(path, encoding="utf-8") as f:
        next(f)
        for raw in f:
            p = raw.rstrip("\n").split("\t")
            seq = int(p[0])
            draft, flag, conf = p[2].strip(), p[3].strip(), p[4].strip()
            if flag == "?":
                unsure.add(seq)
            if conf:
                gold[seq] = None if conf == "-" else conf
            elif use_draft:
                gold[seq] = draft or None
    return gold, unsure


def build_cfg(provider, model, variant):
    return ac.ClassifyCfg(provider=provider, model=model, **VARIANTS[variant])


class Recorder:
    """모델 호출을 감싸 호출 수와 입력 글자 수를 센다. dry-run 이면 모델을 부르지 않고 에코 답을 쓴다."""

    def __init__(self, inner, dry):
        self.inner, self.dry = inner, dry
        self.calls, self.in_chars, self.lines = 0, 0, 0

    def __call__(self, user):
        self.calls += 1
        self.in_chars += len(ac.SYSTEM_PROMPT) + len(user)
        self.lines += user.count("\n[", user.find("[분류할 줄]")) + 1
        return ac.echo_llm(user) if self.dry else self.inner(user)


def replay(lines, batches, agendas_msg, cfg, dry):
    ad = adapt_agendas(agendas_msg)
    rec = Recorder(ac.make_llm(cfg), dry)
    cls = ac.AgendaClassifier(ad["meeting_id"], cfg, llm=rec)
    cls.set_agendas(ad["version"], ad["agendas"])
    for b in batches:
        cls.receive(b)
        cls.advance(b["sent_ms"])
        cls.process()
    cls.advance(batches[-1]["sent_ms"] + 1000)
    cls.process(final=True)
    pred = {seq: (cls.orig.get(k, k) if k is not None else None) for seq, k in cls.labels.items()}
    return pred, rec, cls.info()


def estimate_cost(rec, model):
    pin, pout = PRICES.get(model, PRICES["gemini-3.8-flash"])
    tin = rec.in_chars * TOK_PER_KO_CHAR
    tout = rec.calls * OUT_TOK_BASE + rec.lines * OUT_TOK_PER_LINE
    usd = tin / 1e6 * pin + tout / 1e6 * pout
    return {"calls": rec.calls, "inTokens": tin, "outTokens": tout, "usd": round(usd, 4), "krw": round(usd * USD_KRW_ASSUMED)}


def score(pred, gold, unsure):
    seqs = sorted(s for s in gold if s in pred)
    missing = sorted(s for s in gold if s not in pred)

    def acc(sel):
        sel = list(sel)
        return {"n": len(sel), "acc": round(sum(1 for s in sel if pred[s] == gold[s]) / len(sel), 4) if sel else None}
    agenda_lines = [s for s in seqs if gold[s] is not None]
    null_lines = [s for s in seqs if gold[s] is None]
    conf = {}
    for s in seqs:
        k = f"{gold[s] or '없음'}->{pred[s] or '없음'}"
        conf[k] = conf.get(k, 0) + 1
    return {"overall": acc(seqs), "agendaLines": acc(agenda_lines), "nullLines": acc(null_lines),
            "confidentOnly": acc(s for s in seqs if s not in unsure), "unsureOnly": acc(s for s in seqs if s in unsure),
            "unanswered": len(missing), "confusion": dict(sorted(conf.items())),
            "wrong": [s for s in seqs if pred[s] != gold[s]]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--agendas", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--provider", default="echo")
    ap.add_argument("--model")
    ap.add_argument("--variants", default="with_context,without_context")
    ap.add_argument("--dry-run", action="store_true", help="모델을 부르지 않고 호출 수와 비용 추정만 낸다")
    ap.add_argument("--paid-ok", action="store_true", help="유료 호출 허용 (기준과 비용을 확인한 뒤에만)")
    ap.add_argument("--use-draft", action="store_true", help="확정 라벨이 없는 줄은 초안으로 채운다 (점검용)")
    ap.add_argument("--out", help="결과 JSON 경로 (유료 실행은 이미 있으면 거부)")
    a = ap.parse_args()

    paid = a.provider != "echo" and not a.dry_run
    if paid and not a.paid_ok:
        sys.exit("유료 호출입니다. 기준과 비용을 확인한 뒤 --paid-ok 를 붙이세요. (--dry-run 으로 추정만 볼 수 있습니다)")
    if paid and not a.model:
        sys.exit("--model 이 필요합니다")
    if paid and (not a.out or os.path.exists(a.out)):
        sys.exit("유료 실행은 --out 이 필요하고, 그 파일이 이미 있으면 다시 돌리지 않습니다 (한 번만)")

    lines = load_jsonl_as_protocol_lines(a.transcript)
    batches = make_batches(lines, period_ms=10000)
    with open(a.agendas, encoding="utf-8") as f:
        agendas_msg = json.load(f)
    gold, unsure = load_labels(a.labels, a.use_draft)
    dry = a.dry_run or a.provider == "echo"
    result = {"provider": a.provider, "model": a.model, "dryRun": dry, "labels": len(gold), "unsure": len(unsure),
              "passAccuracy": PASS_ACCURACY, "variants": {}}
    total = {"usd": 0.0, "krw": 0, "calls": 0}
    for v in a.variants.split(","):
        t0 = time.time()
        pred, rec, info = replay(lines, batches, agendas_msg, build_cfg(a.provider, a.model, v), dry)
        r = {"info": info, "cost": estimate_cost(rec, a.model), "seconds": round(time.time() - t0, 1)}
        if not a.dry_run:
            r["score"] = score(pred, gold, unsure)
        result["variants"][v] = r
        total["usd"] += r["cost"]["usd"]; total["krw"] += r["cost"]["krw"]; total["calls"] += r["cost"]["calls"]
    result["totalEstimate"] = {"calls": total["calls"], "usd": round(total["usd"], 4), "krw": total["krw"]}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)


if __name__ == "__main__":
    main()
