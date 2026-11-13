"""챗봇 평가 (SCRUM-223·224·225·249). 대화가 이어지는 질문에서 ① 질문을 고쳐 쓰기 전·후의 검색 순위와 ② 답이 맞는지를 잰다.

  .venv/bin/python -m rag.eval_chat rag/tests/fixtures/chat_eval_two_meetings.json --dry-run     AI 없이 점검·고치기 전 순위
  .venv/bin/python -m rag.eval_chat rag/tests/fixtures/chat_eval_two_meetings.json --runs 10     실제 AI. 호출마다 요금
  .venv/bin/python -m rag.eval_chat --summary data/eval/chat/chat_eval_two_meetings-20261112-150000
  .venv/bin/python -m rag.eval_chat rag/tests/fixtures/chat_eval_live_summary.json --runs 1 --no-summary   실시간 요약을 빼고 (전후 비교)

판마다 사례마다: 서버와 같은 함수로 질문을 고쳐 쓰고(rewrite_query) → 진행 중 회의 근거(live_evidence·summary_evidence, 서버와 같음)
+ 고친 질문으로 검색한 상위 5개를 근거로 → 서버와 같은 글(chat_input)과 판정(judge_chat)으로 답한다. 검색은 eval_search 처럼 실제 임베딩 모델로 메모리에서 한다
(DB 를 쓰지 않음). 고치기 전 순위는 원래 질문으로 한 번만 잰다 (AI 를 쓰지 않아 판마다 같다).
AI 는 서버와 같은 설정이다 (RAG_LLM_*, 환경변수가 없으면 저장소 루트 .env). 폴백은 쓰지 않는다. 키 값은 출력하지 않는다.
결과는 data/eval/chat/{사례}-{시각}/ 에 남는다 (git 이 무시함): meta.json, runs.jsonl (판마다 고친 질문·순위·답 글·채점).
고친 질문과 답 글을 남기므로 합성 회의록만 쓴다. 실제 회의 데이터로 돌린 결과는 레포에 올리지 않는다.

사례 파일 (JSON):
  {"about": "설명",
   "docs": {"A": {"file": "minutes_confirmed_table.json", "title": "주간 회의 (10/20)", "docId": 1},   file 은 사례 파일 기준
            "R": {"file": "chat_project/requirements.pdf", "title": "요구사항 정의서", "docId": 11}},   json 은 회의록, 그 밖은 첨부 자료
   "live": "chat_project/live_1112.json",       진행 중 회의 (선택). {currentAgendaId, liveAgendas, recentLines, liveSummary}
   "userId": 55, "userName": "한지우",          질문한 사용자 (선택, 사례마다 바꿀 수 있음)
   "now": "2026-11-12T15:40:00+09:00",          <지금> 칸에 넣을 시각 (선택, 없으면 이 값. 판마다 같게 고정)
   "cases": [{"id": "C1", "type": "앞 답을 가리킴",
              "history": [{"role": "user", "text": "..."}, {"role": "assistant", "text": "... [1]."}],
              "q": "그거 언제까지야?",
              "gold": [["A", "QA 일정표"]],
              "expect": "answered",
              "mustSay": [["수요일"]], "mustNotSay": []}]}

  history    백엔드가 보내는 대화 (오래된 것부터). 비우면 고쳐 쓰지 않는다
  gold       찾아와야 할 조각 [문서, 글 일부]. 그 문서의 조각 중 글에 이 말이 든 것이 꼭 하나여야 한다
             진행 중 회의는 ["LIVE", 글 일부] (보드 항목·전사 줄 묶음 중 꼭 하나. 검색 지표에는 넣지 않는다)
             no_result 사례는 비운다 (검색 지표에서 뺀다)
  expect     answered 또는 no_result. 둘 다 맞으면 ["answered", "no_result"] (gold 는 비워도 된다)
  noLive     true 면 이 사례에는 진행 중 회의를 넘기지 않는다
  noSummary  true 면 이 사례에는 실시간 요약을 넘기지 않는다 (--no-summary 는 모든 사례)
  basis      answered 일 때 맞아야 하는 근거 종류 sources / general / mixed (선택). general 이면 gold 를 비운다
  receivedAt 문서마다 (첨부 자료). 사례 파일 맨 위 "receivedAtAll" 이 있으면 모든 첨부 자료에 그 값을 쓴다
  mustSay    묶음마다 그중 한 말이 답에 있어야 한다 (묶음끼리는 모두)
  mustNotSay 답에 있으면 틀린 말
  맞음       상태가 expect 와 같고(목록이면 그중 하나), answered 이면 gold 가 있을 때 그중 하나 이상을 근거로 쓰고
             basis 가 있으면 같고, mustSay·mustNotSay 를 지킴
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

from . import live as live_mod
from .chat import (DOC_TOP_K, Budget, ChatRequest, Evidence, Turn, chat_input, chat_prompts, judge_chat,
                   live_evidence, recent_turns, rewrite_query, summary_evidence)
from .eval_answer import Recorder, real_llm
from .dates import when_label
from .eval_search import build_index, rank_all
from .loaders.minutes import meeting_time

REPO = Path(__file__).resolve().parent.parent
OUT_ROOT = REPO / "data" / "eval" / "chat"
EXPECTS = ("answered", "no_result")
BASES = ("sources", "general", "mixed")
DEFAULT_NOW = "2026-11-12T15:40:00+09:00"
LIVE_KEY = "LIVE"


class CaseError(ValueError):
    """사례 파일이 틀림"""


# ---------- 사례 ----------
def load_case(path: Path) -> dict:
    """문서마다 근거 머리에 붙일 날짜 글(when)을 서버와 같은 함수로 만든다 (SCRUM-234).
    회의록은 파일의 meetingStartAt, 첨부 자료는 사례 파일의 receivedAt (서버가 그 버전을 받은 시각에 해당)."""
    case = json.loads(path.read_text(encoding="utf-8"))
    for d in case["docs"].values():
        if case.get("receivedAtAll") and not d["file"].endswith(".json"):
            d["receivedAt"] = case["receivedAtAll"]
        d["file"] = str(path.parent / d["file"])
        if d["file"].endswith(".json"):
            minutes = json.loads(Path(d["file"]).read_text(encoding="utf-8"))
            d["when"] = when_label("minutes", meeting_time(minutes), None)
        else:
            received = datetime.fromisoformat(d["receivedAt"]) if d.get("receivedAt") else None
            d["when"] = when_label("upload", None, received)
    if case.get("live"):
        case["liveData"] = json.loads((path.parent / case["live"]).read_text(encoding="utf-8"))
    return case


def expects(c) -> tuple[str, ...]:
    e = c.get("expect")
    return tuple(e) if isinstance(e, list) else (e,)


def doc_gold(c) -> list:
    return [k for k in c.get("gold", []) if k[0] != LIVE_KEY]


def live_gold(c) -> list:
    return [k for k in c.get("gold", []) if k[0] == LIVE_KEY]


def user_of(case, c):
    return c.get("userId", case.get("userId", 1)), c.get("userName", case.get("userName"))


def live_request(case, c, query) -> ChatRequest:
    data = {} if c.get("noLive") else case.get("liveData") or {}
    uid, name = user_of(case, c)
    summary = () if c.get("noSummary") or case.get("noSummaryAll") else tuple(data.get("liveSummary") or ())
    return ChatRequest(workspace_id=1, meeting_id=data.get("meetingId", 1), user_id=uid, query=query, docs=(),
                       user_name=name, current_agenda_id=data.get("currentAgendaId"),
                       live_agendas=tuple(data.get("liveAgendas") or ()), recent_lines=tuple(data.get("recentLines") or ()),
                       live_summary=summary)


def live_ev_of(case, c, query, embedder):
    """서버와 같은 순서: 안건 보드 → 실시간 요약 → 전사 줄. (안건 보드, 요약, 전사 줄)."""
    r = live_request(case, c, query)
    board, lines = live_evidence(r)
    summary = summary_evidence(r, embedder, query) if r.live_summary else []
    return board, summary, lines


def live_all_texts(case, c) -> list[str]:
    """고르기 전 진행 중 회의 근거 글 전부 (사례 점검용. 넘쳐서 빠진 항목도 들어 있다).
    실시간 요약을 빼고 돌릴 때(noSummary, --no-summary)도 요약을 넣고 본다. 정답이 사례 파일에 있는지를 보는 것이므로."""
    r = live_request({**case, "noSummaryAll": False}, {**c, "noSummary": False}, "")
    items = (live_mod.board_items(r.live_agendas, r.current_agenda_id, r.user_id) + live_mod.summary_items(r.live_summary)
             + live_mod.line_bundles(r.recent_lines))
    return [i.text for i in items]


def live_gold_refs(c, live_ev) -> list[int]:
    """넘긴 진행 중 회의 근거 중 gold 에 맞는 번호 (1부터). 넘쳐서 빠졌으면 없다."""
    out = []
    for _, needle in live_gold(c):
        out += [i for i, e in enumerate(live_ev, 1) if needle in e.text]
    return out


def gold_index(chunks, key) -> int:
    doc, needle = key
    found = [i for i, (k, c) in enumerate(chunks) if k == doc and needle in c.content]
    if len(found) != 1:
        raise CaseError(f"정답 조각 고르기 실패: {doc} {needle!r} → {len(found)}개")
    return found[0]


def check_case(chunks, c, live_texts=()) -> list[str]:
    problems = []
    exp = expects(c)
    if not exp or any(e not in EXPECTS for e in exp):
        problems.append(f"{c['id']}: expect 는 {' 또는 '.join(EXPECTS)} (또는 그 목록)")
    if exp == ("no_result",) and (c.get("gold") or c.get("mustSay")):
        problems.append(f"{c['id']}: no_result 사례에 gold·mustSay 가 있음")
    if c.get("basis") is not None and c["basis"] not in BASES:
        problems.append(f"{c['id']}: basis 는 {' / '.join(BASES)}")
    if c.get("basis") == "general" and c.get("gold"):
        problems.append(f"{c['id']}: basis 가 general 인데 gold 가 있음")
    if exp == ("answered",) and not c.get("gold") and c.get("basis") != "general":
        problems.append(f"{c['id']}: answered 사례에 gold 가 없음")
    for _, needle in live_gold(c):
        n = sum(needle in t for t in live_texts)
        if n != 1:
            problems.append(f"{c['id']}: 진행 중 회의 정답 고르기 실패: {needle!r} → {n}개")
    for key in doc_gold(c):
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
        return [pos[gold_index(self.chunks, k)] for k in doc_gold(c)]

    def evidence(self, order) -> list[Evidence]:
        out = []
        for i in order[:DOC_TOP_K]:
            key, chunk = self.chunks[i]
            d = self.case["docs"][key]
            source = "minutes" if d["file"].endswith(".json") else "upload"        # 회의록은 json, 첨부 자료는 pdf·docx·xlsx
            out.append(Evidence(source, d["docId"], d["title"], chunk.content, chunk.location, d.get("when", "")))
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


def score(c, text, status, refs, gold_refs, basis=None) -> dict:
    s = {"status": status, "basis": basis, "refs": refs, "goldCited": sorted(set(refs) & set(gold_refs)),
         "missingSay": [], "badSay": [], "needGold": bool(c.get("gold"))}
    if status == "answered":
        s["missingSay"] = [g for g in c.get("mustSay", []) if not any(says(text, w) for w in g)]
        s["badSay"] = [w for w in c.get("mustNotSay", []) if says(text, w)]
    s["badBasis"] = status == "answered" and c.get("basis") is not None and basis != c["basis"]
    s["ok"] = (status in expects(c) and not s["missingSay"] and not s["badSay"] and not s["badBasis"]
               and (status != "answered" or not c.get("gold") or bool(s["goldCited"])))
    return s


# ---------- 실행 ----------
def run_case(case, index, llm, runs, prompts, out_dir, timeout_s, rewrite_timeout_s, progress=print) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = Recorder(llm)
    base = {c["id"]: index.ranks(c, index.order(c["q"])) for c in case["cases"]}
    meta = {"about": case.get("about"), "live": case.get("live"), "noSummary": bool(case.get("noSummaryAll")), "model": f"{rec.name}/{rec.model}", "runs": runs,
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
    case = index.case
    turns = turns_of(c)
    before = len(rec.calls)
    rw = rewrite_query(rec, prompts.rewrite, turns, c["q"], rewrite_timeout_s)
    rw_calls = rec.calls[before:]
    board, summary, lines = live_ev_of(case, c, rw.query, index.embedder)
    live_ev = board + summary + lines
    order = index.order(rw.query)
    ranks = index.ranks(c, order)
    evidence = live_ev + index.evidence(order)
    wanted = {gold_index(index.chunks, k) for k in doc_gold(c)}
    gold_refs = live_gold_refs(c, live_ev) + [len(live_ev) + i for i, r in enumerate(order[:DOC_TOP_K], 1) if r in wanted]
    _, user_name = user_of(case, c)
    now = datetime.fromisoformat(c.get("now", case.get("now", DEFAULT_NOW)))
    before = len(rec.calls)
    try:
        reply = rec.generate(prompts.chat.text, chat_input(c["q"], turns, evidence, rw.query, user_name, now),
                             timeout_s)
        text = (reply.text or "").strip()
        status, refs, basis, _ = judge_chat(text, len(evidence))
        s = score(c, text, status, refs, gold_refs, basis)
    except Exception as e:
        text, s = None, {"status": "failed", "refs": [], "ok": False, "error": type(e).__name__}
    return {"case": c["id"], "run": n, "rewrite": {"query": rw.query, "state": rw.state, "calls": rw_calls},
            "ranks": ranks, "live": {"board": len(board), "summary": len(summary), "lines": len(lines), "goldPassed": bool(live_gold_refs(c, live_ev))
                                     if live_gold(c) else None},
            "text": text, "score": s, "calls": rec.calls[before:]}


# ---------- 요약 ----------
def summarize(out_dir: Path) -> str:
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in (out_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines() if x]
    base = meta["baseRanks"]
    out = [f"모델 {meta['model']} / 지시문 {' · '.join(meta['prompts'])} / {meta['runs']}판", "",
           "| 사례 | 고치기 전 순위 | 고친 뒤 상위 5 안 (판) | 고친 질문 (첫 판) | 답 맞음 | answered / no_result / failed "
           "| 근거 종류 (sources / general / mixed) |",
           "|---|---|---|---|---|---|---|"]
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
                   f"{st.count('answered')} / {st.count('no_result')} / {st.count('failed')} | "
                   f"{' / '.join(str(sum(r['score'].get('basis') == b for r in rs)) for b in BASES)} |")
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
    if s["status"] == "answered" and not s.get("goldCited") and s.get("needGold", True):
        whys.append("정답 조각을 근거로 쓰지 않음")
    if s.get("missingSay"):
        whys.append(f"말 빠짐 {s['missingSay']}")
    if s.get("badSay"):
        whys.append(f"나오면 안 될 말 {s['badSay']}")
    if s.get("badBasis"):
        whys.append(f"근거 종류 {s.get('basis')}")
    return ", ".join(whys) or f"상태 {s['status']}"


# ---------- 점검 ----------
def dry_run(case, index, out=print) -> bool:
    ok = True
    for c in case["cases"]:
        problems = check_case(index.chunks, c, live_all_texts(case, c))
        out(f"\n{c['id']} ({c.get('type', '')}) 대화 {len(c.get('history', []))}개 / {c['q']}  → 정답 {c.get('expect')}")
        if problems:
            ok = False
            out("".join(f"  ✘ {p}\n" for p in problems).rstrip())
            continue
        if doc_gold(c):
            out(f"  고치기 전 정답 조각 순위: {', '.join(map(str, index.ranks(c, index.order(c['q']))))}")
        if live_gold(c):
            board, summary, lines = live_ev_of(case, c, c["q"], index.embedder)
            passed = bool(live_gold_refs(c, board + summary + lines))
            out(f"  진행 중 회의 근거 보드 {len(board)} + 요약 {len(summary)} + 전사 {len(lines)}개, "
                f"정답 {'넘김' if passed else '넘치어 빠짐'}")
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
    p.add_argument("--no-summary", action="store_true", help="모든 사례에서 실시간 요약을 넘기지 않는다 (전후 비교)")
    p.add_argument("--summary", type=Path, metavar="결과폴더", help="저장된 결과로 요약만 다시 낸다")
    args = p.parse_args(argv)
    if args.summary:
        print(summarize(args.summary))
        return
    if not args.case:
        p.error("사례 파일이 필요함")
    case = load_case(args.case)
    case["noSummaryAll"] = args.no_summary
    from .embedding import E5Embedder
    print("임베딩 모델 불러오는 중 ...", end=" ", flush=True)
    index = Index(case, E5Embedder(threads=2))
    print(f"조각 {len(index.chunks)}개")
    if args.dry_run:
        sys.exit(0 if dry_run(case, index) else 1)
    problems = [x for c in case["cases"] for x in check_case(index.chunks, c, live_all_texts(case, c))]
    if problems:
        sys.exit("사례 파일 문제:\n" + "\n".join(problems))
    llm = real_llm()
    prompts = chat_prompts(args.chat_prompt, args.rewrite_prompt)
    out_dir = OUT_ROOT / f"{args.case.stem}{'-nosummary' if args.no_summary else ''}-{datetime.now():%Y%m%d-%H%M%S}"
    print(f"사례: {args.case.name} / AI: {llm.name} ({llm.model}) / {args.runs}판 / 사례 {len(case['cases'])}개 / "
          f"{prompts.chat.version} · {prompts.rewrite.version}", flush=True)
    t0 = time.monotonic()
    run_case(case, index, llm, args.runs, prompts, out_dir, args.timeout, args.rewrite_timeout,
             progress=lambda s: print(s, flush=True))
    print(f"\n결과: {out_dir} ({time.monotonic() - t0:.0f}초)\n")
    print(summarize(out_dir))


if __name__ == "__main__":
    main()
