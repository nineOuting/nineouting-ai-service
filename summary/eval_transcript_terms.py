#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
전사 속 용어 평가 (REQ-191, SCRUM-295)

합성 대본을 실제 서비스처럼 10초 묶음으로 흘려 보내며 용어 찾기(transcript_terms)를 돌리고, 사람이 확정한 정답표와 맞춰 본다.
용어집은 비워 둔다. 용어집 길은 모델을 쓰지 않아 코드 시험으로 확인했고, 여기서는 모델 길(AI 설명)만 본다.
안건은 넣지 않는다 (안건 제목 검사는 이 평가의 대상이 아니다).

자동으로 재는 것
  - 맞는 비율: 찾은 용어 중 정답표의 must + may 비율
  - 빠뜨린 비율: must 중 찾은 비율 (재현율)
  - 오경보: must_not 을 찾은 수. 정답표에 없는 말은 "기타"로 따로 세고 사람이 판단한다
  - 잘못 받아쓴 말을 설명한 수 (정답표 misheard)
  - 코드가 버린 후보 수, 호출 수, 실패 수, 입력·출력 토큰 (유료 실행만. 모델이 알려 준 값)
사람이 채점표(--grading-out)로 확인하는 것: 뜻이 맞는가, 회의 속 사실(이름·숫자·결정)이 섞였는가, 편드는가.

결과 JSON(--out)에는 숫자만 넣는다. 채점표에는 찾은 용어와 뜻을 넣는다 (합성 대본이라 실제 회의 내용이 아니다).

사용 (저장소 루트에서)
  # 배선 점검 (비용 0, 모델을 부르지 않음): 호출 수와 입력 글자 수
  .venv/bin/python summary/eval_transcript_terms.py --dry-run
  # 실제 모델 (유료. 기준과 비용을 확인한 뒤에만). 키는 환경변수 또는 저장소 루트 .env 에서 읽는다
  .venv/bin/python summary/eval_transcript_terms.py --provider openai --model gpt-6-luna --runs 3 --paid-ok \
      --out data/eval/terms/r1.json --grading-out data/eval/terms/r1_grading.md
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import meeting_summary  # noqa: E402
from meeting_summary import load_jsonl_as_protocol_lines, make_batches  # noqa: E402
from agenda_classifier import AgendaClassifier, ClassifyCfg  # noqa: E402
from transcript_terms import TranscriptTerms, Glossary, TermsCfg, _norm  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures", "transcript_terms")
SCRIPTS = ["A_product", "B_dev", "C_general"]
PERIOD_MS = 10000                   # 백엔드가 줄 묶음을 보내는 주기 (input-contract)

CRITERIA = {                        # 기준 (사용자 확정 11/18). 넘지 못하면 지시문을 고치고 다시 잰다
    "precision_min": 0.8,           # 찾은 용어 중 must + may
    "recall_min": 0.6,              # must 중 찾은 비율
    "false_alarm_per_script_max": 1.0,
    "false_alarm_C_max": 0,         # 용어가 없는 회의
    "misheard_explained_max": 0,
}


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


class Recorder:
    """모델 호출을 감싸 호출 수, 입력 글자 수를 센다. dry-run 이면 모델 대신 빈 답을 준다."""

    def __init__(self, real_llm, dry):
        self.real, self.dry = real_llm, dry
        self.calls, self.input_chars = 0, 0

    def __call__(self, user):
        self.calls += 1
        self.input_chars += len(user)
        if self.dry:
            return json.dumps({"terms": []})
        return self.real(user)


def ensure_fixtures(names):
    """대본(jsonl)은 저장소 규칙(.gitignore 의 *.jsonl)으로 깃에 올라가지 않는다. 없으면 make_fixtures.py 로 만든다 (고정 문장이라 늘 같다)."""
    if all(os.path.exists(os.path.join(FIXTURES, f"{n}.jsonl")) for n in names):
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_fixtures", os.path.join(FIXTURES, "make_fixtures.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.check()
    mod.write()
    print("합성 대본이 없어 make_fixtures.py 로 만들었습니다", file=sys.stderr)


def run_script(name, cfg, dry):
    lines = load_jsonl_as_protocol_lines(os.path.join(FIXTURES, f"{name}.jsonl"))
    for ln in lines:
        ln.setdefault("manual", False)
    c = AgendaClassifier(f"eval-{name}", ClassifyCfg())
    h = TranscriptTerms(c, Glossary([]), cfg)
    rec = Recorder(h.llm, dry)
    h.llm = rec
    for b in make_batches(lines, period_ms=PERIOD_MS):
        c.receive(b)
        c.advance(b["sent_ms"])
        h.scan_glossary()
        h.process()
    h.process(final=True)
    sys_chars = len(__import__("transcript_terms").SYSTEM_PROMPT)
    return {"terms": h.terms(), "info": h.info(), "calls": rec.calls, "input_chars": rec.input_chars + rec.calls * sys_chars,
            "audio_sec": max(ln["end_ms"] for ln in lines) / 1000, "lines": len(lines)}


def score(found, ans):
    cat = {}
    for col in ("must", "may", "must_not"):
        for w in ans[col]:
            cat[_norm(w)] = col
    heard = {_norm(m["heard"]) for m in ans["misheard"]}
    got = {}
    for t in found:
        k = _norm(t["term"])
        got[k] = cat.get(k, "other")
    must = [_norm(w) for w in ans["must"]]
    n = len(got)
    good = sum(1 for v in got.values() if v in ("must", "may"))
    return {
        "found": n,
        "must_found": sum(1 for k in must if k in got),
        "must_total": len(must),
        "may_found": sum(1 for v in got.values() if v == "may"),
        "false_alarm": sum(1 for v in got.values() if v == "must_not"),
        "other": sum(1 for v in got.values() if v == "other"),
        "misheard_explained": sum(1 for k in got if k in heard),
        "precision": round(good / n, 3) if n else None,
        "recall": round(sum(1 for k in must if k in got) / len(must), 3) if must else None,
    }, got


def main():
    ap = argparse.ArgumentParser(description="전사 속 용어 평가 (SCRUM-295)")
    ap.add_argument("--provider", default="echo")
    ap.add_argument("--model")
    ap.add_argument("--runs", type=int, default=1, help="대본마다 몇 번 돌릴지 (결과가 들쭉날쭉한지 본다)")
    ap.add_argument("--scripts", default=",".join(SCRIPTS))
    ap.add_argument("--dry-run", action="store_true", help="모델을 부르지 않고 호출 수와 입력 글자 수만 낸다 (비용 0)")
    ap.add_argument("--paid-ok", action="store_true", help="유료 호출 허용 (기준과 비용을 확인한 뒤에만)")
    ap.add_argument("--out", help="결과 JSON (숫자만). 유료 실행은 이미 있으면 거부")
    ap.add_argument("--grading-out", help="사람 채점표 (찾은 용어와 뜻, 합성 대본)")
    a = ap.parse_args()

    paid = a.provider != "echo" and not a.dry_run
    if paid and not a.paid_ok:
        sys.exit("유료 호출입니다. 기준과 비용을 확인한 뒤 --paid-ok 를 붙이세요. (--dry-run 으로 호출 수만 볼 수 있습니다)")
    if paid and not a.model:
        sys.exit("--model 이 필요합니다")
    if paid and a.out and os.path.exists(a.out):
        sys.exit(f"결과 파일이 이미 있습니다: {a.out} (한 번만 돌립니다)")
    if paid:
        load_dotenv(os.path.join(HERE, "..", ".env"))

    tokens = {"in": 0, "out": 0}
    if paid:
        meeting_summary.USAGE_HOOK = lambda p, m, task, tin, tout: (tokens.__setitem__("in", tokens["in"] + tin),
                                                                     tokens.__setitem__("out", tokens["out"] + tout))
    ensure_fixtures(a.scripts.split(","))
    with open(os.path.join(FIXTURES, "answers.json"), encoding="utf-8") as f:
        answers = json.load(f)

    results, grading = [], []
    for name in a.scripts.split(","):
        for run in range(1, a.runs + 1):
            before = dict(tokens)
            cfg = TermsCfg(provider=a.provider if not a.dry_run else "echo", model=a.model)
            r = run_script(name, cfg, a.dry_run)
            s, got = score(r["terms"], answers[name])
            row = {"script": name, "run": run, **s, "calls": r["calls"], "errors": r["info"]["errors"], "rejected": r["info"]["rejected"],
                   "halted": r["info"]["halted"], "input_chars": r["input_chars"], "audio_sec": r["audio_sec"], "lines": r["lines"],
                   "tokens_in": tokens["in"] - before["in"], "tokens_out": tokens["out"] - before["out"]}
            results.append(row)
            print(f"{name} #{run}: 찾음 {s['found']} (must {s['must_found']}/{s['must_total']}, may {s['may_found']}, 오경보 {s['false_alarm']}, "
                  f"기타 {s['other']}, 오인식 설명 {s['misheard_explained']}) 맞는 비율 {s['precision']} 재현율 {s['recall']} | "
                  f"호출 {r['calls']} 실패 {row['errors']} 버림 {row['rejected']} 입력 {r['input_chars']}자"
                  + (f" 토큰 {row['tokens_in']}/{row['tokens_out']}" if paid else ""))
            for t in r["terms"]:
                grading.append((name, run, got[_norm(t["term"])], t["term"], t["text"]))

    summary = summarize(results, paid)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"provider": a.provider, "model": a.model, "dry_run": a.dry_run, "criteria": CRITERIA, "summary": summary,
                       "runs": results}, f, ensure_ascii=False, indent=2)
    if a.grading_out:
        os.makedirs(os.path.dirname(os.path.abspath(a.grading_out)), exist_ok=True)
        with open(a.grading_out, "w", encoding="utf-8") as f:
            f.write("| 대본 | 판 | 정답표 | 용어 | 뜻 | 뜻 맞음 | 회의 사실 섞임 | 편듦 |\n|---|---|---|---|---|---|---|---|\n")
            for name, run, c, term, text in grading:
                f.write(f"| {name} | {run} | {c} | {term} | {text} |  |  |  |\n")


def summarize(results, paid):
    by = {}
    for r in results:
        by.setdefault(r["script"], []).append(r)
    out = {"scripts": {}}
    for name, rs in by.items():
        n = len(rs)
        prec = [r["precision"] for r in rs if r["precision"] is not None]
        rec = [r["recall"] for r in rs if r["recall"] is not None]
        out["scripts"][name] = {
            "runs": n,
            "found_avg": round(sum(r["found"] for r in rs) / n, 2),
            "precision_avg": round(sum(prec) / len(prec), 3) if prec else None,
            "recall_avg": round(sum(rec) / len(rec), 3) if rec else None,
            "false_alarm_avg": round(sum(r["false_alarm"] for r in rs) / n, 2),
            "other_avg": round(sum(r["other"] for r in rs) / n, 2),
            "misheard_explained": sum(r["misheard_explained"] for r in rs),
            "calls_avg": round(sum(r["calls"] for r in rs) / n, 2),
            "audio_min": round(rs[0]["audio_sec"] / 60, 2),
            "input_chars_avg": round(sum(r["input_chars"] for r in rs) / n),
            **({"tokens_in_avg": round(sum(r["tokens_in"] for r in rs) / n), "tokens_out_avg": round(sum(r["tokens_out"] for r in rs) / n)}
               if paid else {}),
        }
    calls = sum(r["calls"] for r in results)
    audio_min = sum(r["audio_sec"] for r in results) / 60
    out["calls_total"] = calls
    out["calls_per_hour_est"] = round(calls / audio_min * 60, 1) if audio_min else None   # 대본의 말 빠르기 기준 추정
    if paid:
        tin = sum(r["tokens_in"] for r in results)
        tout = sum(r["tokens_out"] for r in results)
        out["tokens_total"] = {"in": tin, "out": tout}
        out["tokens_per_hour_est"] = {"in": round(tin / audio_min * 60), "out": round(tout / audio_min * 60)} if audio_min else None
    return out


if __name__ == "__main__":
    main()
