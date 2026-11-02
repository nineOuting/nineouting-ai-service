"""답변 지시문 평가. 질문마다 정해 둔 조각을 AI 에 넘겨 여러 번 답하게 하고, 정답표와 맞는지 세고, 지시문 버전끼리 비교한다.

  .venv/bin/python -m rag.eval_answer rag/tests/fixtures/answer_eval_basic.json --dry-run
  .venv/bin/python -m rag.eval_answer rag/tests/fixtures/answer_eval_two_meetings.json --runs 20
  .venv/bin/python -m rag.eval_answer 사례.json --runs 20 --prompt v3 v4
  .venv/bin/python -m rag.eval_answer --summary data/eval/answer/answer_eval_basic-20261102-101500
  .venv/bin/python -m rag.eval_answer 사례.json --rescore data/eval/answer/answer_eval_basic-20261102-101500

검색은 하지 않는다. 사례에 적은 조각을 적은 순서대로 넘기므로, 지시문 차이만 본다 (검색 품질은 eval_search).
조각은 실제 로더(rag.loaders.minutes)로 만들고, AI 에 가는 글과 판정은 답변 API 와 같은 함수(build_prompt, judge_reply)를 쓴다.
--dry-run 은 AI 를 부르지 않는다. 조각이 하나씩 골라지는지와 정답표가 넘기는 조각을 가리키는지만 본다.
버전을 여럿 주면 판마다 번갈아 돌린다.
--rescore 는 저장된 답 글을 지금 사례 파일의 정답표로 다시 채점해 요약만 낸다 (AI 를 부르지 않는다. 결과 파일은 그대로).
AI 는 서버와 같은 설정이다 (RAG_LLM_PROVIDER·RAG_LLM_MODEL·키, 환경변수가 없으면 rag/.env). 키 값은 출력하지 않는다.
결과는 data/eval/answer/{사례}-{시각}/ 에 남는다 (git 이 무시함): meta.json, calls.csv(호출마다 시간·토큰), runs.jsonl(판마다 답 글과 채점).
답 글을 남기므로 합성 회의록만 쓴다. 실제 회의 데이터로 돌린 결과는 레포에 올리지 않는다.

사례 파일 (JSON):
  {"about": "설명",
   "docs": {"A": {"file": "minutes_confirmed_table.json", "title": "주간 회의 (10/20)", "docId": 1}},   file 은 사례 파일 기준
   "questions": [{"id": "D1", "q": "1차 배포 날짜가 어떻게 바뀌었나요?", "type": "두 회의",
                  "pieces": [["A", "10월 30일"], ["B", "11월 3일"], ...],
                  "expect": "answered",
                  "mustCite": [["A", "10월 30일"], ["B", "11월 3일"]],
                  "mayCite": [],
                  "mustNotCite": [],
                  "mustSay": [["10월 30일"], ["11월 3일"]],
                  "mustNotSay": ["1억"]}]}

  pieces       넘길 조각 [문서, 글 일부]. 그 문서의 조각 중 글에 이 말이 든 것이 꼭 하나여야 한다. 적은 순서가 [1], [2] 번호다
  expect       answered 또는 no_result
  mustCite     답이 근거로 꼭 써야 할 조각 (pieces 안의 것). 그 밖의 조각을 쓴 것은 틀림이 아니고 따로 센다
  mayCite      근거로 써도 되는 조각 (질문과 관계있지만 꼭 쓰지 않아도 됨). 따로 세지 않는다
  mustNotCite  근거로 쓰면 틀린 조각 (예: 지시문만 든 조각)
  mustSay      묶음마다 그중 한 말이 답에 있어야 한다 (묶음끼리는 모두)
  mustNotSay   답에 있으면 틀린 말
  답이 answered 일 때만 근거·말을 본다 (no_result 는 화면에 글이 나가지 않는다)
"""
import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from .answer import ANSWER_KIND, answer_prompt, build_prompt, judge_reply, prompt_versions
from .loaders.minutes import load_minutes
from .search import Hit

REPO = Path(__file__).resolve().parent.parent
OUT_ROOT = REPO / "data" / "eval" / "answer"
ENV_FILE = Path(__file__).parent / ".env"
TIMEOUT_S = 30.0                  # 운영 기본값 (RAG_LLM_TIMEOUT_S)
EXPECTS = ("answered", "no_result")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
_TRAILING = re.compile(r"(\s*\[\d+(?:\s*,\s*\d+)*\])+\s*[.!?]*\s*$|\s*[.!?]+\s*$")


class CaseError(ValueError):
    """사례 파일이 틀림 (조각을 하나로 고를 수 없음, 정답표가 넘기지 않는 조각을 가리킴 등)"""


# ---------- 사례 ----------
def load_case(path: Path) -> dict:
    case = json.loads(path.read_text(encoding="utf-8"))
    for doc in case["docs"].values():
        doc["pieces"] = load_minutes(json.loads((path.parent / doc["file"]).read_text(encoding="utf-8"))).pieces
    return case


def pick(case: dict, key) -> object:
    doc, needle = key
    if doc not in case["docs"]:
        raise CaseError(f"없는 문서: {doc}")
    found = [p for p in case["docs"][doc]["pieces"] if needle in p.content]
    if len(found) != 1:
        raise CaseError(f"조각 고르기 실패: {doc} {needle!r} → {len(found)}개")
    return found[0]


def hits_for(case: dict, question: dict) -> list[Hit]:
    hits = []
    for doc, needle in question["pieces"]:
        p, d = pick(case, (doc, needle)), case["docs"][doc]
        hits.append(Hit(doc_id=d["docId"], source_type="minutes", title=d["title"], content=p.content,
                        location=p.location, score=0.0))
    return hits


def ref_of(question: dict, key) -> int:
    """정답표의 [문서, 글 일부] → 넘기는 조각 번호 (1부터)."""
    try:
        return [tuple(k) for k in question["pieces"]].index(tuple(key)) + 1
    except ValueError:
        raise CaseError(f"{question['id']}: 정답표의 {key} 가 pieces 에 없음") from None


def check_question(case: dict, question: dict) -> list[str]:
    """사례 한 질문의 문제를 글로 돌려준다 (없으면 빈 목록)."""
    problems = []
    try:
        hits_for(case, question)
        for field in ("mustCite", "mayCite", "mustNotCite"):
            for key in question.get(field, []):
                ref_of(question, key)
    except CaseError as e:
        problems.append(str(e))
    if question.get("expect") not in EXPECTS:
        problems.append(f"{question['id']}: expect 는 {' 또는 '.join(EXPECTS)}")
    if question.get("expect") == "no_result" and (question.get("mustCite") or question.get("mustSay")):
        problems.append(f"{question['id']}: no_result 질문에 mustCite·mustSay 가 있음")
    keys = [tuple(k) for k in question["pieces"]]
    if len(set(keys)) != len(keys):
        problems.append(f"{question['id']}: 같은 조각을 두 번 넘김")
    return problems


# ---------- 채점 ----------
def plain_sentences(text: str) -> int:
    """합니다체가 아닌 문장 수 ("~다" 로 끝나는데 "~니다" 가 아닌 것). 목록 항목처럼 "다" 로 끝나지 않는 줄은 세지 않는다."""
    n = 0
    for s in _SENTENCE_END.split(text or ""):
        s = _TRAILING.sub("", s.strip())
        if s.endswith("다") and not s.endswith("니다"):
            n += 1
    return n


def score(question: dict, text: str, status: str, refs: list[int]) -> dict:
    s = {"status": status, "refs": refs, "missingCite": [], "badCite": [], "missingSay": [], "badSay": [],
         "extraCites": [], "plain": 0}
    if status == "answered":
        want = {ref_of(question, k) for k in question.get("mustCite", [])}
        may = {ref_of(question, k) for k in question.get("mayCite", [])}
        bad = {ref_of(question, k) for k in question.get("mustNotCite", [])}
        s["missingCite"] = sorted(want - set(refs))
        s["badCite"] = sorted(bad & set(refs))
        s["extraCites"] = sorted(set(refs) - want - may - bad)
        s["missingSay"] = [g for g in question.get("mustSay", []) if not any(w in text for w in g)]
        s["badSay"] = [w for w in question.get("mustNotSay", []) if w in text]
        s["plain"] = plain_sentences(text)
    s["ok"] = status == question["expect"] and not (s["missingCite"] or s["badCite"] or s["missingSay"] or s["badSay"])
    return s


# ---------- 실행 ----------
class Recorder:
    """AI 호출을 감싸 호출마다 시간·토큰·오류를 남긴다."""

    def __init__(self, llm):
        self.llm, self.calls = llm, []
        self.name, self.model = llm.name, getattr(llm, "model", "-")

    def generate(self, system, user, timeout_s):
        rec = {"error": ""}
        t0 = time.monotonic()
        try:
            reply = self.llm.generate(system, user, timeout_s)
        except Exception as e:
            rec.update(ms=_ms(t0), error=type(e).__name__)
            self.calls.append(rec)
            raise
        rec.update(ms=_ms(t0), input_tokens=reply.input_tokens, output_tokens=reply.output_tokens)
        self.calls.append(rec)
        return reply


def _ms(since):
    return round((time.monotonic() - since) * 1000)


def run_case(case: dict, llm, runs: int, versions: list[str | None], out_dir: Path, timeout_s: float = TIMEOUT_S,
             progress=print) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    prompts = [answer_prompt(v) for v in versions]
    rec = Recorder(llm)
    meta = {"about": case.get("about"), "model": f"{rec.name}/{rec.model}", "runs": runs,
            "variants": [p.version for p in prompts], "startedAt": datetime.now().isoformat(timespec="seconds")}
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    fields = ["variant", "question", "run", "ms", "input_tokens", "output_tokens", "error"]
    with open(out_dir / "calls.csv", "w", newline="", encoding="utf-8") as f, \
            open(out_dir / "runs.jsonl", "w", encoding="utf-8") as runs_out:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for n in range(1, runs + 1):
            for prompt in prompts:                         # 버전을 번갈아 돌려 시간대 차이를 고르게
                for q in case["questions"]:
                    _one(case, q, rec, prompt, n, timeout_s, writer, runs_out)
                f.flush()
                runs_out.flush()
            progress(f"{n}/{runs} 판 끝")
    return out_dir


def _one(case, q, rec, prompt, n, timeout_s, writer, runs_out):
    hits = hits_for(case, q)
    before = len(rec.calls)
    try:
        reply = rec.generate(prompt.text, build_prompt(q["q"], hits), timeout_s)
        text = (reply.text or "").strip()
        status, refs, _ = judge_reply(text, len(hits))
        s = score(q, text, status, refs)
    except Exception as e:                                 # 서버에서는 failed / llm_failed·llm_timeout
        text, s = None, {"status": "failed", "refs": [], "ok": False, "error": type(e).__name__}
    calls = rec.calls[before:]
    for c in calls:
        writer.writerow({"variant": prompt.version, "question": q["id"], "run": n, **c})
    runs_out.write(json.dumps({"variant": prompt.version, "question": q["id"], "run": n, "text": text,
                               "score": s, "ms": sum(c["ms"] for c in calls), "calls": calls},
                              ensure_ascii=False) + "\n")


# ---------- 요약 ----------
def summarize(out_dir: Path, case: dict | None = None) -> str:
    """case 를 주면 저장된 답 글을 그 정답표로 다시 채점한다 (--rescore)."""
    rows = [json.loads(line) for line in (out_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if case is not None:
        rows = [rescore(case, r) for r in rows]
    out = ["| 질문 | 지시문 | 맞음 | answered / no_result / failed | 관계없는 조각 인용 | 합니다체 아님 |",
           "|---|---|---|---|---|---|"]
    groups, misses = {}, []
    for r in rows:
        groups.setdefault((r["question"], r["variant"]), []).append(r)
    for (qid, variant), rs in groups.items():
        st = [r["score"]["status"] for r in rs]
        extra = sum(len(r["score"].get("extraCites", [])) for r in rs)
        plain = sum(r["score"].get("plain", 0) for r in rs)
        ok = sum(r["score"]["ok"] for r in rs)
        out.append(f"| {qid} | {variant} | {ok}/{len(rs)} | {st.count('answered')} / {st.count('no_result')} / "
                   f"{st.count('failed')} | {extra} | {plain} |")
        counts = {}
        for r in rs:
            for why in _why_wrong(r["score"]):
                counts[why] = counts.get(why, 0) + 1
        misses += [f"- {qid} / {variant}: {why} ({c}번)" for why, c in sorted(counts.items())]
    out += ["", "| 지시문 | 맞음 | 시간 중앙/최대(초) | 토큰 입력/출력 (호출 평균) |", "|---|---|---|---|"]
    for variant in dict.fromkeys(r["variant"] for r in rows):
        rs = [r for r in rows if r["variant"] == variant]
        calls = [c for r in rs for c in r["calls"] if not c.get("error")]
        secs = [c["ms"] / 1000 for c in calls] or [0]
        tin = statistics.mean([c.get("input_tokens") or 0 for c in calls]) if calls else 0
        tout = statistics.mean([c.get("output_tokens") or 0 for c in calls]) if calls else 0
        out.append(f"| {variant} | {sum(r['score']['ok'] for r in rs)}/{len(rs)} | "
                   f"{statistics.median(secs):.1f} / {max(secs):.1f} | {tin:.0f} / {tout:.0f} |")
    out += ["", "틀린 것 (이유별):", *(misses or ["- 없음"])]
    return "\n".join(out)


def rescore(case: dict, row: dict) -> dict:
    """저장된 한 판을 지금 정답표로 다시 채점한다. AI 호출이 실패한 판(답 글 없음)은 그대로 둔다."""
    if row["text"] is None:
        return row
    q = next((q for q in case["questions"] if q["id"] == row["question"]), None)
    if q is None:
        raise CaseError(f"결과의 질문 {row['question']} 이 사례 파일에 없음")
    status, refs, _ = judge_reply(row["text"], len(q["pieces"]))
    return {**row, "score": score(q, row["text"], status, refs)}


def _why_wrong(s: dict) -> list[str]:
    if s["ok"]:
        return []
    if s["status"] == "failed":
        return [f"failed {s.get('error') or '(범위 밖 번호)'}"]
    whys = []
    if s.get("missingCite"):
        whys.append(f"근거 빠짐 {s['missingCite']}")
    if s.get("badCite"):
        whys.append(f"쓰면 안 될 근거 {s['badCite']}")
    if s.get("missingSay"):
        whys.append(f"말 빠짐 {s['missingSay']}")
    if s.get("badSay"):
        whys.append(f"나오면 안 될 말 {s['badSay']}")
    return whys or [f"상태 {s['status']}"]


# ---------- 점검 ----------
def dry_run(case: dict, out=print) -> bool:
    ok = True
    for q in case["questions"]:
        problems = check_question(case, q)
        out(f"\n{q['id']} ({q.get('type', '')}) {q['q']}  → 정답 {q.get('expect')}")
        if problems:
            ok = False
            out("".join(f"  ✘ {p}\n" for p in problems).rstrip())
            continue
        cite = {ref_of(q, k) for k in q.get("mustCite", [])}
        may = {ref_of(q, k) for k in q.get("mayCite", [])}
        nocite = {ref_of(q, k) for k in q.get("mustNotCite", [])}
        for i, h in enumerate(hits_for(case, q), 1):
            mark = "근거" if i in cite else "허용" if i in may else "금지" if i in nocite else "    "
            out(f"  {mark} [{i}] ({h.title}, {h.location.get('label', '')}) {h.content}")
        if q.get("mustSay") or q.get("mustNotSay"):
            out(f"  말: {q.get('mustSay', [])}  금지: {q.get('mustNotSay', [])}")
    out("\n점검 " + ("통과" if ok else "실패"))
    return ok


# ---------- 실제 AI ----------
def real_llm():
    from .llm import LLMConfigError, make_llm
    from .try_answer import load_env_file

    env = {**load_env_file(ENV_FILE), **os.environ}
    try:
        llm, _ = make_llm(env)                         # 시간 제한은 이 도구의 --timeout 을 쓴다
    except LLMConfigError as e:
        sys.exit(f"실제 AI 설정 오류: {e}")
    if llm.name == "fake":
        sys.exit("RAG_LLM_PROVIDER 가 fake 다. rag/.env 에 gemini 나 openai 를 넣어야 실제 AI 로 돈다")
    return llm


def _resolved(version):
    """버전을 주지 않으면 지금 가장 큰 번호를 적어 둔다 (결과에 어느 버전인지 남게)."""
    return version or prompt_versions(ANSWER_KIND)[-1]


def main(argv=None):
    p = argparse.ArgumentParser(description="답변 지시문 평가")
    p.add_argument("case", type=Path, nargs="?", help="사례 파일 (JSON)")
    p.add_argument("--runs", type=int, default=20)
    p.add_argument("--prompt", nargs="+", default=[None], help="답변 지시문 버전 (예: v3 v4). 없으면 가장 큰 번호")
    p.add_argument("--timeout", type=float, default=TIMEOUT_S)
    p.add_argument("--dry-run", action="store_true", help="AI 없이 조각 고르기와 정답표만 본다")
    p.add_argument("--summary", type=Path, metavar="결과폴더", help="저장된 결과로 요약만 다시 낸다")
    p.add_argument("--rescore", type=Path, metavar="결과폴더", help="저장된 답 글을 사례 파일의 정답표로 다시 채점한다")
    args = p.parse_args(argv)
    if args.summary:
        print(summarize(args.summary))
        return
    if not args.case:
        p.error("사례 파일이 필요함")
    case = load_case(args.case)
    if args.rescore:
        print(summarize(args.rescore, case))
        return
    if args.dry_run:
        sys.exit(0 if dry_run(case) else 1)
    problems = [x for q in case["questions"] for x in check_question(case, q)]
    if problems:
        sys.exit("사례 파일 문제:\n" + "\n".join(problems))
    llm = real_llm()
    versions = [_resolved(v) for v in args.prompt]
    out_dir = OUT_ROOT / f"{args.case.stem}-{datetime.now():%Y%m%d-%H%M%S}"
    print(f"사례: {args.case.name} / AI: {llm.name} ({llm.model}) / {args.runs}판 / 질문 {len(case['questions'])}개 / "
          + ", ".join(f"answer-{v}" for v in versions), flush=True)
    run_case(case, llm, args.runs, versions, out_dir, args.timeout, progress=lambda s: print(s, flush=True))
    print(f"\n결과: {out_dir}\n")
    print(summarize(out_dir))


if __name__ == "__main__":
    main()
