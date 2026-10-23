#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
안건 상태 판정 평가 (AI-04)

끝난 회의 전사를 실제 서비스처럼 10초 묶음으로 흘려 보내고, 정해 둔 시점(체크포인트)과 회의 종료 때의 안건 상태를
사람이 확정한 정답 상태와 맞춰 본다. 분류 입력을 두 가지로 돌려 상태 판정이 분류 오류를 얼마나 물려받는지 잰다.
  gold : 사람이 확정한 분류(labels_confirmed.tsv)를 그대로 쓴다. 판정기 자체의 품질만 잰다. 분류 모델은 부르지 않는다.
  ai   : AI-02 분류기가 낸 분류를 쓴다. 분류 호출 비용이 따로 든다. 참고용.

재는 것
  - 상태 일치율: 체크포인트마다 안건 상태가 정답과 같은 비율 (엄격: 정답 상태만, 너그럽게: alsoOk 도 인정)
  - 결정 누락: 종료 때 정답이 DECIDED 인데 DECIDED 로 내지 못한 안건 수 (PRD 제안 기준 회의당 1건 이하)
  - 잘못된 결정: 정답이 DECIDED 가 아닌데 DECIDED 로 낸 안건 수 (0이어야 함)
  - 결정 확인 필요 정밀도: DECISION_CHECK 로 낸 안건 중 정답이 DECIDED 나 DECISION_CHECK 인 비율
  - 근거 줄: 낸 근거가 그 안건의 정답 줄인 비율, 정답 근거와 겹치는지
  - 호출 수, 비용 추정
결과 JSON 에는 전사와 이유 문장을 넣지 않는다 (상태와 줄 번호만).

사용
  # 모델 호출 없이 호출 수와 비용 추정만 (기본, 비용 0)
  python3 eval_agenda_status.py --transcript T.jsonl --agendas A.json --labels L.tsv --truth S.json --dry-run --provider gemini --model gemini-3.8-flash
  # 에코 모의 판정기로 스크립트 점검 (비용 0)
  python3 eval_agenda_status.py ... --provider echo --use-draft
  # 유료 평가 (한 번만. 결과 파일이 이미 있으면 거부) -- 키는 환경변수로만
  python3 eval_agenda_status.py ... --provider gemini --model gemini-3.8-flash --paid-ok --out R.json

정답 파일(JSON)은 사람이 확정해야 한다. confirmed 가 true 가 아니면 --use-draft 일 때만 쓴다 (점검용, 최종 수치 아님).
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
from input_adapter import adapt_agendas  # noqa: E402
import agenda_classifier as ac  # noqa: E402
import agenda_status as ast  # noqa: E402
import eval_agenda as ea  # noqa: E402
import eval_agenda_summary as es  # noqa: E402

CRITERIA = {                        # 제안 기준. 실행 전에 확인받는다
    "statusAgreementStrict": 0.80,  # 체크포인트 x 안건 중 정답 상태와 같은 비율 (gold)
    "maxMissedDecisions": 1,        # 종료 때 정답 DECIDED 를 DECIDED 로 내지 못한 수 (PRD 제안값)
    "maxFalseDecided": 0,           # 정답이 DECIDED 가 아닌데 DECIDED 로 낸 수
    "evidenceInAgenda": 0.90,       # 근거 줄이 그 안건의 정답 줄인 비율
}
OUT_TOK_PER_CALL = 90               # 호출 한 번의 출력 (판정, 이유 한 문장, 근거) 추정
MODES = ("gold", "ai")


class StatusRecorder:
    """상태 판정 모델 호출을 감싸 호출 수와 입력 글자 수를 센다. dry 이면 모델을 부르지 않고 에코 답을 쓴다."""

    def __init__(self, inner, dry):
        self.inner, self.dry = inner, dry
        self.calls, self.in_chars = 0, 0

    def __call__(self, user):
        self.calls += 1
        self.in_chars += len(ast.SYSTEM_PROMPT) + len(user)
        return ast.echo_llm(user) if self.dry else self.inner(user)


def load_dotenv(path):
    """저장소 루트의 .env 에서 아직 설정되지 않은 환경변수만 읽어 온다 (값은 출력하지 않는다)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


def load_truth(path, use_draft):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not d.get("confirmed") and not use_draft:
        sys.exit("정답 상태 파일이 아직 확정되지 않았습니다 (confirmed 가 true 가 아님). 사람이 확인한 뒤 true 로 바꾸세요. 점검용이면 --use-draft")
    points = list(d["checkpoints"]) + [dict(d["end"], atMs=None)]
    return points, bool(d.get("confirmed"))


SHOW_UNCERTAIN = False        # --show-uncertain: 낮은 확신도 줄을 [불확실] 표시로 보여 주고 판정한다 (기본은 예전처럼 뺀다)


def replay(batches, agendas_msg, gold, mode, provider, model, dry, points):
    """묶음을 흘려 보내며 체크포인트마다, 그리고 종료 때 board_status 를 모은다."""
    ad = adapt_agendas(agendas_msg)
    ccfg = ac.ClassifyCfg(provider=provider, model=model)
    crec = ea.Recorder(ac.make_llm(ccfg), dry)
    cls = ac.AgendaClassifier(ad["meeting_id"], ccfg, llm=crec)
    cls.set_agendas(ad["version"], ad["agendas"])
    if mode == "gold":                                      # 사람이 확정한 분류를 고정하고 분류 모델은 부르지 않는다
        cls.apply_corrections([{"seq": s, "agenda_id": a, "rev": 1} for s, a in gold.items()])
        cls.halted = "평가: 사람이 확정한 분류만 사용"
    scfg = ast.StatusCfg(provider=provider, model=model, show_uncertain=SHOW_UNCERTAIN)
    srec = StatusRecorder(ast.make_llm(scfg), dry)
    judge = ast.AgendaStatusJudge(cls, scfg, llm=srec)
    ids = {a["key"]: cls._id(a["key"]) for a in cls.agendas}
    timed = [p for p in points if p.get("atMs") is not None]
    snaps = {}

    def snap(pid):
        snaps[pid] = {ids[k]: v for k, v in judge.board_status().items()}

    for b in batches:
        while timed and b["sent_ms"] > timed[0]["atMs"]:    # 이 묶음이 체크포인트 시각을 넘기 전의 상태
            snap(timed.pop(0)["id"])
        cls.receive(b)
        cls.advance(b["sent_ms"])
        cls.process()
        judge.process()
    for p in timed:
        snap(p["id"])
    cls.advance(batches[-1]["sent_ms"] + 1000)
    cls.process(final=True)
    judge.process(final=True)
    snap("END")
    return snaps, cls, judge, crec, srec


def score(snaps, points, gold):
    gold_by_agenda = {}
    for s, a in gold.items():
        if a:
            gold_by_agenda.setdefault(a, set()).add(s)
    pairs, strict, lenient, per_point = 0, 0, 0, {}
    for p in points:
        got = snaps[p["id"]]
        row = {}
        for aid, t in p["statuses"].items():
            pred = got[aid]["status"]
            ok_s = pred == t["status"]
            ok_l = ok_s or pred in t.get("alsoOk", [])
            pairs += 1; strict += ok_s; lenient += ok_l
            row[aid] = {"truth": t["status"], "pred": pred, "strict": ok_s, "lenient": ok_l}
        per_point[p["id"]] = row
    end = next(p for p in points if p["id"] == "END")
    missed, false_dec, check_total, check_ok, ev_total, ev_in, ev_overlap, ev_need = [], [], 0, 0, 0, 0, 0, 0
    for aid, t in end["statuses"].items():
        g = snaps["END"][aid]
        pred = g["status"]
        if t["status"] == "DECIDED" and pred != "DECIDED":
            missed.append(aid)
        if pred == "DECIDED" and t["status"] != "DECIDED":
            false_dec.append(aid)
        if pred == "DECISION_CHECK":
            check_total += 1
            check_ok += t["status"] in ("DECIDED", "DECISION_CHECK")
    for p in points:                                        # 근거는 모든 체크포인트의 DECIDED, DECISION_CHECK 로 본다
        for aid, t in p["statuses"].items():
            g = snaps[p["id"]][aid]
            if g["status"] in ("DECIDED", "DECISION_CHECK") and g["reason"]:
                seqs = g["reason"]["evidenceSeqs"]
                ev_total += len(seqs)
                ev_in += sum(1 for s in seqs if s in gold_by_agenda.get(aid, set()))
                if t.get("evidenceSeqs") and t["status"] == g["status"]:
                    ev_need += 1
                    ev_overlap += bool(set(seqs) & set(t["evidenceSeqs"]))
    return {"pairs": pairs, "statusAgreementStrict": round(strict / pairs, 4), "statusAgreementLenient": round(lenient / pairs, 4),
            "missedDecisions": missed, "falseDecided": false_dec,
            "checkPrecision": round(check_ok / check_total, 4) if check_total else None, "checkPredicted": check_total,
            "evidenceInAgenda": round(ev_in / ev_total, 4) if ev_total else None, "evidenceLines": ev_total,
            "evidenceOverlapTruth": f"{ev_overlap}/{ev_need}", "perCheckpoint": per_point}


def judge_pass(sc, mode):
    c = CRITERIA
    r = {"maxMissedDecisions": len(sc["missedDecisions"]) <= c["maxMissedDecisions"],
         "maxFalseDecided": len(sc["falseDecided"]) <= c["maxFalseDecided"]}
    if mode == "gold":                                      # ai 모드는 분류가 틀리면 달라지므로 참고용
        r["statusAgreementStrict"] = sc["statusAgreementStrict"] >= c["statusAgreementStrict"]
        r["evidenceInAgenda"] = sc["evidenceInAgenda"] is None or sc["evidenceInAgenda"] >= c["evidenceInAgenda"]
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--agendas", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--provider", default="echo")
    ap.add_argument("--model")
    ap.add_argument("--modes", default="gold,ai")
    ap.add_argument("--dry-run", action="store_true", help="모델을 부르지 않고 호출 수와 비용 추정만 낸다")
    ap.add_argument("--paid-ok", action="store_true", help="유료 호출 허용 (기준과 비용을 확인한 뒤에만)")
    ap.add_argument("--show-uncertain", action="store_true", help="낮은 확신도 줄을 [불확실] 표시를 달아 모델에 보여 준다(기본은 빼기). 전후 비교용")
    ap.add_argument("--use-draft", action="store_true", help="확정 전 정답·라벨 초안을 쓴다 (점검용)")
    ap.add_argument("--out", help="결과 JSON 경로 (유료 실행은 이미 있으면 거부)")
    a = ap.parse_args()
    global SHOW_UNCERTAIN
    SHOW_UNCERTAIN = a.show_uncertain

    paid = a.provider != "echo" and not a.dry_run
    if paid and not a.paid_ok:
        sys.exit("유료 호출입니다. 기준과 비용을 확인한 뒤 --paid-ok 를 붙이세요. (--dry-run 으로 추정만 볼 수 있습니다)")
    if paid and not a.model:
        sys.exit("--model 이 필요합니다")
    if paid:
        load_dotenv(os.path.join(HERE, "..", ".env"))
        if a.provider == "gemini" and not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            sys.exit("GEMINI_API_KEY 를 찾지 못했습니다 (환경변수와 저장소 루트 .env 모두 비어 있음). 호출하지 않고 끝냅니다")
    if paid and (not a.out or os.path.exists(a.out)):
        sys.exit("유료 실행은 --out 이 필요하고, 그 파일이 이미 있으면 다시 돌리지 않습니다 (한 번만)")
    modes = a.modes.split(",")
    if any(m not in MODES for m in modes):
        sys.exit(f"--modes 는 {', '.join(MODES)} 중에서 고르세요")

    lines = load_jsonl_as_protocol_lines(a.transcript)
    batches = make_batches(lines, period_ms=10000)
    with open(a.agendas, encoding="utf-8") as f:
        agendas_msg = json.load(f)
    gold, _unsure = ea.load_labels(a.labels, a.use_draft)
    points, confirmed = load_truth(a.truth, a.use_draft)
    dry = a.dry_run or a.provider == "echo"
    result = {"provider": a.provider, "model": a.model, "dryRun": dry, "showUncertain": a.show_uncertain, "labels": len(gold), "truthConfirmed": confirmed,
              "criteria": CRITERIA, "modes": {}}
    total = {"usd": 0.0, "krw": 0, "calls": 0}
    for mode in modes:
        t0 = time.time()
        snaps, cls, judge, crec, srec = replay(batches, agendas_msg, gold, mode, a.provider, a.model, dry, points)
        st_cost = es.cost(srec.in_chars, srec.calls * OUT_TOK_PER_CALL, a.model)
        cls_cost = ea.estimate_cost(crec, a.model) if crec.calls else None
        r = {"statusCalls": srec.calls, "statusCost": st_cost, "classifyCost": cls_cost, "statusInfo": judge.info(),
             "seconds": round(time.time() - t0, 1)}
        broken = judge.halted or (cls.halted if mode == "ai" else None)
        if broken and not dry:                              # 호출이 연속 실패로 멈췄으면 점수를 내지 않는다 (규칙 판정만 남아 수치가 틀려진다)
            r["invalid"] = f"모델 호출이 멈춰 점수를 내지 않음: {broken}"
        elif not a.dry_run:
            r["score"] = score(snaps, points, gold)
            r["pass"] = judge_pass(r["score"], mode)
        result["modes"][mode] = r
        total["usd"] += st_cost["usd"] + (cls_cost["usd"] if cls_cost else 0)
        total["krw"] += st_cost["krw"] + (cls_cost["krw"] if cls_cost else 0)
        total["calls"] += srec.calls + crec.calls
    result["totalEstimate"] = {"calls": total["calls"], "usd": round(total["usd"], 4), "krw": total["krw"]}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    if any("invalid" in r for r in result["modes"].values()):
        print("\n[경고] 모델 호출이 멈춘 모드가 있어 결과가 유효하지 않습니다. 원인을 고친 뒤 새 파일 이름으로 다시 돌려야 합니다.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
