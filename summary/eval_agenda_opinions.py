#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
안건별 의견·할 일 정리 평가 (AI-05)

끝난 회의 전사를 실제 서비스처럼 10초 묶음으로 흘려 보내고, 회의가 끝났을 때 안건 보드에 담긴 의견과 할 일을
사람이 확정한 정답 항목과 맞춰 본다. 분류는 사람이 확정한 분류(gold)를 그대로 쓰므로 분류 모델은 부르지 않는다.
항목 짝짓기는 근거 줄이 겹치는지로 한다: 정답 항목의 근거 줄 번호 중 가장 작은 것부터 가장 큰 것까지(그 안건의 줄만)를
정답 범위로 보고, 모델 항목의 근거 줄이 이 범위와 한 줄이라도 겹치면 같은 항목으로 센다.

재는 것
  의견 : 정답 의견을 찾은 비율(재현율), 낸 의견 중 정답 범위와 겹친 비율(정밀도), 근거 줄이 그 안건의 줄인 비율
  할 일: 같은 세 가지 + 할 일이 정답에 없는 안건에 낸 할 일 수 + 담당자·기한을 채운 수(이 회의엔 말로 나온 것이 없다)
  가짜 회의(--synthetic): 이름·기한이 말로 나오는 짧은 지어낸 대화로 담당자 이름 맞추기와 지어내기를 본다 (호출 1~2번)
결과 JSON 에는 전사와 모델이 쓴 문장을 넣지 않는다 (개수와 줄 번호만). 문장은 --texts-out 에 따로 쓴다 (커밋 금지 폴더에).

사용
  # 모델 호출 없이 호출 수와 비용 추정만 (비용 0)
  python3 eval_agenda_opinions.py --transcript T.jsonl --agendas A.json --labels L.tsv --truth I.json --dry-run --provider gemini --model gemini-3.8-flash
  # 에코 모의 모델로 스크립트 점검 (비용 0)
  python3 eval_agenda_opinions.py ... --provider echo --use-draft
  # 유료 평가 (한 번만. 결과 파일이 이미 있으면 거부). 키는 저장소 루트 .env 에서 읽는다
  python3 eval_agenda_opinions.py ... --provider gemini --model gemini-3.8-flash --paid-ok --out R.json --texts-out data/eval_v2/x.txt

정답 파일(JSON)과 분류 라벨은 사람이 확정해야 한다. 확정 전 초안은 --use-draft 일 때만 쓴다 (점검용, 최종 수치 아님).
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
import agenda_opinions as ao  # noqa: E402
import agenda_todos as at  # noqa: E402
import eval_agenda as ea  # noqa: E402
import eval_agenda_summary as es  # noqa: E402

CRITERIA = {                         # 제안 기준. 실행 전에 확인받는다
    "opinions": {"recall": 0.70, "precision": 0.70, "evidenceInAgenda": 0.90},
    "todos": {"recall": 0.60, "precision": 0.50, "evidenceInAgenda": 0.90,
              "maxExtraInNoTodoAgendas": 1, "maxFilledAssigneeOrDue": 1},
    "synthetic": {"expectedFound": 4, "maxInvented": 0},     # 지어낸 대화에서 기대한 4건을 모두 찾고 지어낸 값이 없어야 한다
}
OUT_TOK_PER_CALL = {"opinions": 400, "todos": 200}           # 호출 한 번의 출력 추정 (최대치에 가깝게 잡음)
TARGETS = ("opinions", "todos")
MODULES = {"opinions": ao, "todos": at}


class Rec:
    """모델 호출을 감싸 호출 수와 입력 글자 수를 센다. dry 이면 모델을 부르지 않고 에코 답을 쓴다."""

    def __init__(self, mod, inner, dry):
        self.mod, self.inner, self.dry = mod, inner, dry
        self.calls, self.in_chars = 0, 0

    def __call__(self, user):
        self.calls += 1
        self.in_chars += len(self.mod.SYSTEM_PROMPT) + len(user)
        return self.mod.echo_llm(user) if self.dry else self.inner(user)


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
        sys.exit("정답 항목 파일이 아직 확정되지 않았습니다 (confirmed 가 true 가 아님). 사람이 확인한 뒤 true 로 바꾸세요. 점검용이면 --use-draft")
    return d["agendas"], bool(d.get("confirmed"))


def make_modules(cls, targets, provider, model, dry, extra=None):
    mods, recs = {}, {}
    for t in targets:
        mod = MODULES[t]
        cfg = (ao.OpinionCfg if t == "opinions" else at.TodoCfg)(provider=provider, model=model, **(extra or {}))
        rec = Rec(mod, None, dry)
        obj = (ao.AgendaOpinions if t == "opinions" else at.AgendaTodos)(cls, cfg, llm=rec)
        rec.inner = obj._make_llm() if not dry else None
        mods[t], recs[t] = obj, rec
    return mods, recs


def replay(batches, agendas_msg, gold, targets, provider, model, dry, extra=None):
    ad = adapt_agendas(agendas_msg)
    ccfg = ac.ClassifyCfg(provider=provider, model=model)
    cls = ac.AgendaClassifier(ad["meeting_id"], ccfg, llm=lambda u: ac.echo_llm(u))
    cls.set_agendas(ad["version"], ad["agendas"])
    cls.apply_corrections([{"seq": s, "agenda_id": a, "rev": 1} for s, a in gold.items()])
    cls.halted = "평가: 사람이 확정한 분류만 사용"          # 분류 모델은 부르지 않는다
    mods, recs = make_modules(cls, targets, provider, model, dry, extra)
    ids = {a["key"]: cls._id(a["key"]) for a in cls.agendas}
    for b in batches:
        cls.receive(b)
        cls.advance(b["sent_ms"])
        cls.process()
        for m in mods.values():
            m.process()
    cls.advance(batches[-1]["sent_ms"] + 1000)
    cls.process(final=True)
    for m in mods.values():
        m.process(final=True)
    board = {t: {ids[k]: v for k, v in m.board_items().items()} for t, m in mods.items()}
    return board, cls, mods, recs


def spans(truth_items, own):
    """정답 항목 -> 정답 범위(그 안건의 줄 중 근거의 최소~최대 사이)."""
    out = []
    for t in truth_items:
        lo, hi = min(t["evidence"]), max(t["evidence"])
        out.append({"id": t["id"], "span": {s for s in own if lo <= s <= hi} | set(t["evidence"])})
    return out


def score(target, board, truth, gold):
    gold_by_agenda = {}
    for s, a in gold.items():
        if a:
            gold_by_agenda.setdefault(a, set()).add(s)
    t_total = t_found = o_total = o_match = ev_total = ev_in = 0
    extra, filled, per = [], 0, {}
    for aid, tr in truth.items():
        own = gold_by_agenda.get(aid, set())
        tspans = spans(tr[target], own)
        outs = board.get(aid, [])
        found = [t["id"] for t in tspans if any(set(o["evidenceSeqs"]) & t["span"] for o in outs)]
        good = [o for o in outs if any(set(o["evidenceSeqs"]) & t["span"] for t in tspans)]
        ev = [s for o in outs for s in o["evidenceSeqs"]]
        evi = [s for s in ev if s in own]
        t_total += len(tspans); t_found += len(found)
        o_total += len(outs); o_match += len(good)
        ev_total += len(ev); ev_in += len(evi)
        if target == "todos":
            if not tspans and outs:
                extra.append(aid)
            filled += sum(1 for o in outs if o.get("assigneeName") or o.get("dueText"))
        per[aid] = {"truth": len(tspans), "found": len(found), "missed": [t["id"] for t in tspans if t["id"] not in found],
                    "output": len(outs), "outputMatched": len(good), "evidenceLines": len(ev), "evidenceInAgenda": len(evi)}
    r = {"truthItems": t_total, "found": t_found, "recall": round(t_found / t_total, 4) if t_total else None,
         "outputItems": o_total, "outputMatched": o_match, "precision": round(o_match / o_total, 4) if o_total else None,
         "evidenceLines": ev_total, "evidenceInAgenda": round(ev_in / ev_total, 4) if ev_total else None, "perAgenda": per}
    if target == "todos":
        r["extraInNoTodoAgendas"] = extra
        r["filledAssigneeOrDue"] = filled
    return r


def judge_pass(target, sc):
    c = CRITERIA[target]
    ok = lambda v, lo: v is None or v >= lo
    r = {"recall": ok(sc["recall"], c["recall"]), "precision": ok(sc["precision"], c["precision"]),
         "evidenceInAgenda": ok(sc["evidenceInAgenda"], c["evidenceInAgenda"])}
    if target == "todos":
        r["maxExtraInNoTodoAgendas"] = len(sc["extraInNoTodoAgendas"]) <= c["maxExtraInNoTodoAgendas"]
        r["maxFilledAssigneeOrDue"] = sc["filledAssigneeOrDue"] <= c["maxFilledAssigneeOrDue"]
    return r


# --------------------------------------------------------------------------------------
# 가짜 회의: 이름과 기한이 말로 나오는 지어낸 대화 (전사가 아니므로 저장소에 둬도 된다)
# --------------------------------------------------------------------------------------
SYN_ROSTER = [{"user_id": 7, "name": "김철수"}, {"user_id": 8, "name": "이영희"}, {"user_id": 9, "name": "박민수"},
              {"user_id": 10, "name": "최민수"}]
SYN_LINES = ["다음 주 배포 일정을 정해야 해요", "철수님이 수요일까지 QA 일정표를 공유해 주세요", "네 알겠습니다 수요일까지 공유할게요",
             "영희 씨는 금요일까지 배포 체크리스트를 정리해 주실 수 있을까요", "네 그렇게 할게요",
             "민수님이 서버 점검을 맡아 주세요", "지호 님이 로그인 화면 맡을게요", "디자인 시안은 누가 좀 확인해 보면 좋겠어요"]
SYN_EXPECT = [("철수", "수요일까지", "MATCHED", 7), ("영희", "금요일까지", "MATCHED", 8),
              ("민수", None, "AMBIGUOUS", None), ("지호", None, "NOT_IN_ROSTER", None)]


def synthetic(provider, model, dry):
    def ln(i):
        t = SYN_LINES[i - 1]
        s = i * 3000
        return {"id": i, "start_ms": s, "end_ms": s + 2000, "arrival_ms": s + 2000, "type": "final", "kind": None,
                "low_confidence": False, "text": t, "text_marked": t, "manual": False}
    c = ac.AgendaClassifier("syn", ac.ClassifyCfg(provider=provider, model=model), llm=lambda u: ac.echo_llm(u))
    c.set_agendas(1, [{"id": "s1", "key": "s1", "position": 1, "title": "배포 일정", "decision_point": "배포 날짜와 담당을 정한다",
                       "added": False}])
    lines = [ln(i) for i in range(1, len(SYN_LINES) + 1)]
    c.receive({"lines": lines, "edits": [], "sent_ms": 30000})
    c.apply_corrections([{"seq": i, "agenda_id": "s1", "rev": 1} for i in range(1, len(SYN_LINES) + 1)])
    c.halted = "평가: 지어낸 대화"
    c.advance(30000)
    rec = Rec(at, None, dry)
    t = at.AgendaTodos(c, at.TodoCfg(provider=provider, model=model), llm=rec)
    rec.inner = t._make_llm() if not dry else None
    t.set_roster([{"user_id": p["user_id"], "name": p["name"]} for p in SYN_ROSTER])
    t.process(final=True)
    out = t.board_items()["s1"]
    return out, t, rec


def synthetic_score(out, info):
    found, invented = [], 0
    for name, due, status, uid in SYN_EXPECT:
        hit = [o for o in out if o.get("assigneeName") == name and o.get("assigneeStatus") == status
               and o.get("assigneeUserId") == uid and (due is None or o.get("dueText") == due)]
        if hit:
            found.append(name)
    for o in out:                                                       # 대화에 없는 이름·기한이 남아 있으면 지어낸 것
        if o.get("assigneeName") and not any(o["assigneeName"] in x for x in SYN_LINES):
            invented += 1
        if o.get("dueText") and not any(o["dueText"] in x for x in SYN_LINES):
            invented += 1
    return {"expected": len(SYN_EXPECT), "found": found, "foundCount": len(found), "invented": invented,
            "outputItems": len(out), "scrubbed": info.get("scrubbed")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--agendas", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--targets", default="opinions,todos")
    ap.add_argument("--synthetic", action="store_true", help="이름·기한이 나오는 지어낸 대화로 담당자 맞추기도 본다")
    ap.add_argument("--min-new-lines", type=int, help="새 줄이 이만큼 쌓이면 바로 호출 (기본 2). 비용을 줄이는 평가용 설정")
    ap.add_argument("--provider", default="echo")
    ap.add_argument("--model")
    ap.add_argument("--dry-run", action="store_true", help="모델을 부르지 않고 호출 수와 비용 추정만 낸다")
    ap.add_argument("--paid-ok", action="store_true", help="유료 호출 허용 (기준과 비용을 확인한 뒤에만)")
    ap.add_argument("--use-draft", action="store_true", help="확정 전 정답·라벨 초안을 쓴다 (점검용)")
    ap.add_argument("--out", help="결과 JSON 경로 (유료 실행은 이미 있으면 거부)")
    ap.add_argument("--texts-out", help="모델이 쓴 항목 문장을 따로 저장할 경로 (커밋하지 않는 폴더에)")
    a = ap.parse_args()

    targets = a.targets.split(",")
    if any(t not in TARGETS for t in targets):
        sys.exit(f"--targets 는 {', '.join(TARGETS)} 중에서 고르세요")
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

    lines = load_jsonl_as_protocol_lines(a.transcript)
    batches = make_batches(lines, period_ms=10000)
    with open(a.agendas, encoding="utf-8") as f:
        agendas_msg = json.load(f)
    gold, _unsure = ea.load_labels(a.labels, a.use_draft)
    truth, confirmed = load_truth(a.truth, a.use_draft)
    dry = a.dry_run or a.provider == "echo"
    t0 = time.time()
    board, cls, mods, recs = replay(batches, agendas_msg, gold, targets, a.provider, a.model, dry,
                                    {"min_new_lines": a.min_new_lines} if a.min_new_lines else None)
    result = {"provider": a.provider, "model": a.model, "dryRun": dry, "labels": len(gold), "truthConfirmed": confirmed,
              "criteria": CRITERIA, "minNewLines": a.min_new_lines or 2, "targets": {}}
    total_krw, total_usd, total_calls = 0, 0.0, 0
    for t in targets:
        rec = recs[t]
        cst = es.cost(rec.in_chars, rec.calls * OUT_TOK_PER_CALL[t], a.model)
        r = {"calls": rec.calls, "cost": cst, "info": mods[t].info()}
        if mods[t].halted and not dry:                       # 호출이 연속 실패로 멈췄으면 점수를 내지 않는다
            r["invalid"] = f"모델 호출이 멈춰 점수를 내지 않음: {mods[t].halted}"
        elif not a.dry_run:
            r["score"] = score(t, board[t], truth, gold)
            r["pass"] = judge_pass(t, r["score"])
        result["targets"][t] = r
        total_krw += cst["krw"]; total_usd += cst["usd"]; total_calls += rec.calls
    syn_out = None
    if a.synthetic and "todos" in targets:
        out, tm, rec = synthetic(a.provider, a.model, dry)
        cst = es.cost(rec.in_chars, rec.calls * OUT_TOK_PER_CALL["todos"], a.model)
        r = {"calls": rec.calls, "cost": cst}
        if tm.halted and not dry:
            r["invalid"] = f"모델 호출이 멈춰 점수를 내지 않음: {tm.halted}"
        elif not a.dry_run:
            r["score"] = synthetic_score(out, tm.info())
            r["pass"] = {"expectedFound": r["score"]["foundCount"] >= CRITERIA["synthetic"]["expectedFound"],
                         "maxInvented": r["score"]["invented"] <= CRITERIA["synthetic"]["maxInvented"]}
        result["synthetic"] = r
        syn_out = out
        total_krw += cst["krw"]; total_usd += cst["usd"]; total_calls += rec.calls
    result["totalEstimate"] = {"calls": total_calls, "usd": round(total_usd, 4), "krw": total_krw}
    result["seconds"] = round(time.time() - t0, 1)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    if a.texts_out and not a.dry_run:
        with open(a.texts_out, "w", encoding="utf-8") as f:
            for t in targets:
                for aid, items in board[t].items():
                    for it in items:
                        body = it.get("text") or it.get("task")
                        extra = f" | 담당={it.get('assigneeName')} {it.get('assigneeStatus')} 기한={it.get('dueText')}" if t == "todos" else f" | {it.get('kind')}"
                        f.write(f"{t} {aid} 근거={it['evidenceSeqs']}{extra}\n  {body}\n")
    if a.texts_out and not a.dry_run and syn_out is not None:    # 지어낸 대화라 문장을 남겨도 된다
        with open(a.texts_out, "a", encoding="utf-8") as f:
            for it in syn_out:
                f.write(f"synthetic 담당={it.get('assigneeName')} {it.get('assigneeStatus')} {it.get('assigneeUserId')} 기한={it.get('dueText')} unclear={it.get('unclear')}\n  {it.get('task')}\n")
    print(text)
    if any("invalid" in r for r in list(result["targets"].values()) + ([result["synthetic"]] if "synthetic" in result else [])):
        print("\n[경고] 모델 호출이 멈춘 항목이 있어 결과가 유효하지 않습니다. 원인을 고친 뒤 새 파일 이름으로 다시 돌려야 합니다.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
