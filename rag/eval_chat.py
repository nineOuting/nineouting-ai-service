"""챗봇 평가 (SCRUM-223). 대화가 이어지는 질문에서 ① 질문을 고쳐 쓰기 전·후의 검색 순위와 ② 답이 맞는지를 잰다.

  .venv/bin/python -m rag.eval_chat rag/tests/fixtures/chat_eval_two_meetings.json --dry-run     AI 없이 점검·고치기 전 순위
  .venv/bin/python -m rag.eval_chat rag/tests/fixtures/chat_eval_two_meetings.json --runs 10     실제 AI. 호출마다 요금
  .venv/bin/python -m rag.eval_chat --summary data/eval/chat/chat_eval_two_meetings-20261112-150000

판마다 사례마다: 서버와 같은 함수로 질문을 고쳐 쓰고(rewrite_query) → 고친 질문으로 검색해 상위 5개를 근거로
→ 서버와 같은 글(chat_input)과 판정(judge_reply)으로 답한다. 검색은 eval_search 처럼 실제 임베딩 모델로 메모리에서 한다
(DB 를 쓰지 않음). 고치기 전 순위는 원래 질문으로 한 번만 잰다 (AI 를 쓰지 않아 판마다 같다).
AI 는 서버와 같은 설정이다 (RAG_LLM_*, 환경변수가 없으면 저장소 루트 .env). 폴백은 쓰지 않는다. 키 값은 출력하지 않는다.
결과는 data/eval/chat/{사례}-{시각}/ 에 남는다 (git 이 무시함): meta.json, runs.jsonl (판마다 고친 질문·순위·답 글·채점).
고친 질문과 답 글을 남기므로 합성 회의록만 쓴다. 실제 회의 데이터로 돌린 결과는 레포에 올리지 않는다.

사례 파일 (JSON):
  {"about": "설명",
   "docs": {"A": {"file": "minutes_confirmed_table.json", "title": "주간 회의 (10/20)", "docId": 1},   file 은 사례 파일 기준
            "R": {"file": "chat_project/requirements.pdf", "title": "요구사항 정의서", "docId": 11}},   json 은 회의록, 그 밖은 첨부 자료
   "cases": [{"id": "C1", "type": "앞 답을 가리킴",
              "history": [{"role": "user", "text": "..."}, {"role": "assistant", "text": "... [1]."}],
              "q": "그거 언제까지야?",
              "gold": [["A", "QA 일정표"]],
              "expect": "answered",
              "mustSay": [["수요일"]], "mustNotSay": []}]}

  history    백엔드가 보내는 대화 (오래된 것부터). 비우면 고쳐 쓰지 않는다
  gold       찾아와야 할 조각 [문서, 글 일부]. 그 문서의 조각 중 글에 이 말이 든 것이 꼭 하나여야 한다
             no_result 사례는 비운다 (검색 지표에서 뺀다)
  expect     answered 또는 no_result
  mustSay    묶음마다 그중 한 말이 답에 있어야 한다 (묶음끼리는 모두)
  mustNotSay 답에 있으면 틀린 말
  맞음       상태가 expect 와 같고, answered 이면 gold 중 하나 이상을 근거로 쓰고 mustSay·mustNotSay 를 지킴
"""
import argparse
import json
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .answer import judge_reply
from .chat import (DOC_TOP_K, Budget, Evidence, Turn, chat_input, chat_prompts, recent_turns,
                   rewrite_query)
from .eval_answer import Recorder, real_llm
from .eval_search import build_index, rank_all

REPO = Path(__file__).resolve().parent.parent
OUT_ROOT = REPO / "data" / "eval" / "chat"
EXPECTS = ("answered", "no_result")


class CaseError(ValueError):
    """사례 파일이 틀림"""


# ---------- 사례 ----------
def load_case(path: Path) -> dict:
    case = json.loads(path.read_text(encoding="utf-8"))
    for d in case["docs"].values():
        d["file"] = str(path.parent / d["file"])
    return case


def gold_index(chunks, key) -> int:
    doc, needle = key
    found = [i for i, (k, c) in enumerate(chunks) if k == doc and needle in c.content]
    if len(found) != 1:
        raise CaseError(f"정답 조각 고르기 실패: {doc} {needle!r} → {len(found)}개")
    return found[0]


def check_case(chunks, c) -> list[str]:
    problems = []
    if c.get("expect") not in EXPECTS:
        problems.append(f"{c['id']}: expect 는 {' 또는 '.join(EXPECTS)}")
    if c.get("expect") == "no_result" and (c.get("gold") or c.get("mustSay")):
        problems.append(f"{c['id']}: no_result 사례에 gold·mustSay 가 있음")
    if c.get("expect") == "answered" and not c.get("gold"):
        problems.append(f"{c['id']}: answered 사례에 gold 가 없음")
    for key in c.get("gold", []):
        try:
            gold_index(chunks, key)
        except CaseError as e:
            problems.append(f"{c['id']}: {e}")
    for i, t in enumerate(c.get("history", [])):
        if t.get("role") not in ("user", "assistant") or not isinstance(t.get("text"), str):
            problems.append(f"{c['id']}: history[{i}] 모양이 틀림")
    return problems


def turns_of(c) -> list[Turn]:
    return recent_turns([Turn(t["role"], t["text"]) for t in c.get("history", [])])


# ---------- 검색 ----------
class Index:
    def __init__(self, case, embedder):
        self.case, self.embedder = case, embedder
        self.chunks, self.vecs = build_index(case["docs"], embedder)

    def order(self, query) -> list[int]:
        return rank_all(self.vecs, np.asarray(self.embedder.embed_query(query), dtype=np.float32))

    def ranks(self, c, order) -> list[int]:
        """gold 마다 순위 (1부터)."""
        pos = {i: r for r, i in enumerate(order, 1)}
        return [pos[gold_index(self.chunks, k)] for k in c.get("gold", [])]

    def evidence(self, order) -> list[Evidence]:
        out = []
        for i in order[:DOC_TOP_K]:
            key, chunk = self.chunks[i]
            d = self.case["docs"][key]
            source = "minutes" if d["file"].endswith(".json") else "upload"        # 회의록은 json, 첨부 자료는 pdf·docx·xlsx
            out.append(Evidence(source, d["docId"], d["title"], chunk.content, chunk.location))
        return out


def hit(ranks, k=DOC_TOP_K) -> bool:
    return any(r <= k for r in ranks)


def rr(ranks) -> float:
    return 1 / min(ranks) if ranks else 0.0


# ---------- 채점 ----------
def says(text, word) -> bool:
    """답에 그 말이 있나. 숫자로 시작하는 말은 앞에 숫자가 붙으면 다른 말로 본다 ("1월 5일" 은 "11월 5일" 에 없다)."""
    if word[:1].isdigit():
        return re.search(r"(?<!\d)" + re.escape(word), text) is not None
    return word in text


def score(c, text, status, refs, gold_refs) -> dict:
    s = {"status": status, "refs": refs, "goldCited": sorted(set(refs) & set(gold_refs)), "missingSay": [],
         "badSay": []}
    if status == "answered":
        s["missingSay"] = [g for g in c.get("mustSay", []) if not any(says(text, w) for w in g)]
        s["badSay"] = [w for w in c.get("mustNotSay", []) if says(text, w)]
    s["ok"] = (status == c["expect"] and not s["missingSay"] and not s["badSay"]
               and (status != "answered" or bool(s["goldCited"])))
    return s


# ---------- 실행 ----------
def run_case(case, index, llm, runs, prompts, out_dir, timeout_s, rewrite_timeout_s, progress=print) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = Recorder(llm)
    base = {c["id"]: index.ranks(c, index.order(c["q"])) for c in case["cases"]}
    meta = {"about": case.get("about"), "model": f"{rec.name}/{rec.model}", "runs": runs,
            "prompts": [prompts.chat.version, prompts.rewrite.version], "embedModel": index.embedder.model_id,
            "baseRanks": base, "startedAt": datetime.now().isoformat(timespec="seconds")}
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(out_dir / "runs.jsonl", "w", encoding="utf-8") as out:
        for n in range(1, runs + 1):
            for c in case["cases"]:
                out.write(json.dumps(_one(c, index, rec, prompts, n, timeout_s, rewrite_timeout_s),
                                     ensure_ascii=False) + "\n")
            out.flush()
            progress(f"{n}/{runs} 판 끝")
    return out_dir


def _one(c, index, rec, prompts, n, timeout_s, rewrite_timeout_s) -> dict:
    turns = turns_of(c)
    before = len(rec.calls)
    rw = rewrite_query(rec, prompts.rewrite, turns, c["q"], rewrite_timeout_s)
    rw_calls = rec.calls[before:]
    order = index.order(rw.query)
    ranks = index.ranks(c, order)
    evidence = index.evidence(order)
    gold_refs = [i for i, r in enumerate(order[:DOC_TOP_K], 1) if r in {gold_index(index.chunks, k) for k in c.get("gold", [])}]
    before = len(rec.calls)
    try:
        reply = rec.generate(prompts.chat.text, chat_input(c["q"], turns, evidence, rw.query), timeout_s)
        text = (reply.text or "").strip()
        status, refs, _ = judge_reply(text, len(evidence))
        s = score(c, text, status, refs, gold_refs)
    except Exception as e:
        text, s = None, {"status": "failed", "refs": [], "ok": False, "error": type(e).__name__}
    return {"case": c["id"], "run": n, "rewrite": {"query": rw.query, "state": rw.state, "calls": rw_calls},
            "ranks": ranks, "text": text, "score": s, "calls": rec.calls[before:]}


# ---------- 요약 ----------
def summarize(out_dir: Path) -> str:
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in (out_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines() if x]
    base = meta["baseRanks"]
    out = [f"모델 {meta['model']} / 지시문 {' · '.join(meta['prompts'])} / {meta['runs']}판", "",
           "| 사례 | 고치기 전 순위 | 고친 뒤 상위 5 안 (판) | 고친 질문 (첫 판) | 답 맞음 | answered / no_result / failed |",
           "|---|---|---|---|---|---|"]
    groups = {}
    for r in rows:
        groups.setdefault(r["case"], []).append(r)
    for cid, rs in groups.items():
        st = [r["score"]["status"] for r in rs]
        b = base.get(cid) or []
        hits = sum(hit(r["ranks"]) for r in rs) if b else None
        first = rs[0]["rewrite"]
        shown = first["query"] if first["state"] == "ok" else f"({first['state']})"
        out.append(f"| {cid} | {', '.join(map(str, b)) or '-'} | {'-' if hits is None else f'{hits}/{len(rs)}'} | "
                   f"{shown} | {sum(r['score']['ok'] for r in rs)}/{len(rs)} | "
                   f"{st.count('answered')} / {st.count('no_result')} / {st.count('failed')} |")
    graded = [r for r in rows if base.get(r["case"])]
    if graded:
        cases = {r["case"] for r in graded}
        b_hit = statistics.mean(hit(base[c]) for c in cases)
        b_mrr = statistics.mean(rr(base[c]) for c in cases)
        a_hit = statistics.mean(hit(r["ranks"]) for r in graded)
        a_mrr = statistics.mean(rr(r["ranks"]) for r in graded)
        out += ["", "| 검색 (정답이 있는 사례) | Hit@5 | MRR |", "|---|---|---|",
                f"| 고치기 전 (원래 질문) | {b_hit:.0%} | {b_mrr:.2f} |",
                f"| 고친 뒤 (판 평균) | {a_hit:.0%} | {a_mrr:.2f} |"]
    out += ["", "| 호출 | 횟수 | 시간 중앙/최대(초) | 토큰 입력/출력 (평균) |", "|---|---|---|---|"]
    for name, calls in (("고쳐 쓰기", [c for r in rows for c in r["rewrite"]["calls"]]),
                        ("답하기", [c for r in rows for c in r["calls"]])):
        ok = [c for c in calls if not c.get("error")]
        secs = [c["ms"] / 1000 for c in ok] or [0]
        tin = statistics.mean([c.get("input_tokens") or 0 for c in ok]) if ok else 0
        tout = statistics.mean([c.get("output_tokens") or 0 for c in ok]) if ok else 0
        out.append(f"| {name} | {len(calls)} (오류 {len(calls) - len(ok)}) | {statistics.median(secs):.1f} / "
                   f"{max(secs):.1f} | {tin:.0f} / {tout:.0f} |")
    misses = [f"- {r['case']} {r['run']}판: {_why(r['score'])}" for r in rows if not r["score"]["ok"]]
    out += ["", "틀린 것:", *(misses or ["- 없음"])]
    return "\n".join(out)


def _why(s) -> str:
    if s["status"] == "failed":
        return f"failed {s.get('error') or '(범위 밖 번호)'}"
    whys = []
    if s["status"] == "answered" and not s.get("goldCited"):
        whys.append("정답 조각을 근거로 쓰지 않음")
    if s.get("missingSay"):
        whys.append(f"말 빠짐 {s['missingSay']}")
    if s.get("badSay"):
        whys.append(f"나오면 안 될 말 {s['badSay']}")
    return ", ".join(whys) or f"상태 {s['status']}"


# ---------- 점검 ----------
def dry_run(case, index, out=print) -> bool:
    ok = True
    for c in case["cases"]:
        problems = check_case(index.chunks, c)
        out(f"\n{c['id']} ({c.get('type', '')}) 대화 {len(c.get('history', []))}개 / {c['q']}  → 정답 {c.get('expect')}")
        if problems:
            ok = False
            out("".join(f"  ✘ {p}\n" for p in problems).rstrip())
            continue
        if c.get("gold"):
            out(f"  고치기 전 정답 조각 순위: {', '.join(map(str, index.ranks(c, index.order(c['q']))))}")
    out("\n점검 " + ("통과" if ok else "실패"))
    return ok


def main(argv=None):
    p = argparse.ArgumentParser(description="챗봇 평가 (질문 고쳐 쓰기 전·후 검색, 답)")
    p.add_argument("case", type=Path, nargs="?", help="사례 파일 (JSON)")
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--chat-prompt", help="답하기 지시문 버전 (예: v1). 없으면 가장 큰 번호")
    p.add_argument("--rewrite-prompt", help="고쳐 쓰기 지시문 버전. 없으면 가장 큰 번호")
    p.add_argument("--timeout", type=float, default=Budget().primary_s, help="답하기 시간 제한 (서버의 기본 모델과 같음)")
    p.add_argument("--rewrite-timeout", type=float, default=Budget().rewrite_s)
    p.add_argument("--dry-run", action="store_true", help="AI 없이 사례 점검과 고치기 전 순위만 본다")
    p.add_argument("--summary", type=Path, metavar="결과폴더", help="저장된 결과로 요약만 다시 낸다")
    args = p.parse_args(argv)
    if args.summary:
        print(summarize(args.summary))
        return
    if not args.case:
        p.error("사례 파일이 필요함")
    case = load_case(args.case)
    from .embedding import E5Embedder
    print("임베딩 모델 불러오는 중 ...", end=" ", flush=True)
    index = Index(case, E5Embedder(threads=2))
    print(f"조각 {len(index.chunks)}개")
    if args.dry_run:
        sys.exit(0 if dry_run(case, index) else 1)
    problems = [x for c in case["cases"] for x in check_case(index.chunks, c)]
    if problems:
        sys.exit("사례 파일 문제:\n" + "\n".join(problems))
    llm = real_llm()
    prompts = chat_prompts(args.chat_prompt, args.rewrite_prompt)
    out_dir = OUT_ROOT / f"{args.case.stem}-{datetime.now():%Y%m%d-%H%M%S}"
    print(f"사례: {args.case.name} / AI: {llm.name} ({llm.model}) / {args.runs}판 / 사례 {len(case['cases'])}개 / "
          f"{prompts.chat.version} · {prompts.rewrite.version}", flush=True)
    t0 = time.monotonic()
    run_case(case, index, llm, args.runs, prompts, out_dir, args.timeout, args.rewrite_timeout,
             progress=lambda s: print(s, flush=True))
    print(f"\n결과: {out_dir} ({time.monotonic() - t0:.0f}초)\n")
    print(summarize(out_dir))


if __name__ == "__main__":
    main()
