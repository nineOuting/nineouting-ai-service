"""회의록 초안 지시문 평가. 합성 요청을 여러 번 돌려 재료가 정답 칸에 들어갔는지 세고, 지시문 버전끼리 비교한다.

  .venv/bin/python -m minutes.eval_draft minutes/tests/fixtures/eval_basic.json --dry-run
  .venv/bin/python -m minutes.eval_draft minutes/tests/fixtures/eval_basic.json --runs 20
  .venv/bin/python -m minutes.eval_draft 사례.json --runs 20 --prompt v8 v9 --relayout-prompt v2 v3
  .venv/bin/python -m minutes.eval_draft --summary data/eval/minutes/eval_basic-20261102-101500

--dry-run 은 AI 를 부르지 않는다. 사례의 정답표가 AI 에 가는 재료와 하나씩 맞물리는지만 본다.
버전을 여럿 주면 판마다 번갈아 돌린다. 개수가 모자란 쪽은 마지막 값을 그대로 쓴다 (예: --prompt v8 v9 --table-prompt v1).
AI 는 손 시험 도구(try_draft --real)와 같은 설정이다 (환경변수, 없으면 rag/.env). 키 값은 출력하지 않는다.
결과는 data/eval/minutes/{사례}-{시각}/ 에 남는다 (git 이 무시함): meta.json, calls.csv(호출마다 시간·토큰), runs.jsonl(판마다 채점과 결과).
실제 회의 데이터로 돌린 결과는 레포에 올리지 않는다.

사례 파일 (JSON):
  {"about": "설명",
   "tasks": [{"name": "배치", "request": "draft_request.json",            요청 경로는 사례 파일 기준
              "expect": [{"source": [101, "decision", 7], "in": ["결정 사항"]},
                         {"source": [103, "decisionCheck", 12], "in": ["미결 안건"], "serious": ["결정 사항"]},
                         {"text": "고객사 로고는", "in": ["할 일", "고객 요청"]}],
              "tables": [{"field": "할 일", "columns": 4, "rows": 3}],
              "then": {"name": "배치-다시배치", "relayoutTo": ["결정 사항", "할 일"], "expect": [...]}}]}

  source  요약 항목 출처 [안건 id, 종류, 항목 id 또는 결정할 사항 id]. 결과 줄의 sources 로 찾는다
  text    출처 없는 줄(작성자가 직접 쓴 줄)을 글 일부로 찾는다
  in      정답 칸 이름들. "(넣지 않음)" 은 어느 칸에도 넣지 않은 것
  serious 이 칸에 들어가면 큰 실수로 따로 센다 (예: 확인 안 된 결정이 결정 칸에)
  then    처음 초안의 줄로 양식을 바꿔 다시 배치한다 (try_draft --relayout-to 와 같은 흐름). 처음이 실패하면 건너뛴다.
          addLines 에 줄(previousLines 모양)을 주면 이전 줄 뒤에 붙인다 (작성자가 직접 쓴 줄)
"""
import argparse
import csv
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

from rag.llm import Reply

from .draft import make_draft
from .materials import build_materials
from .models import TABLE
from .placement import RELAYOUT_KIND, load_prompt
from .relayout import build_relayout, previous_lines_from
from .request import template_fields
from .table import TABLE_KIND

REPO = Path(__file__).resolve().parent.parent
OUT_ROOT = REPO / "data" / "eval" / "minutes"
NONE = "(넣지 않음)"
TIMEOUT_S = 60.0                  # 운영 기본값 (MINUTES_LLM_TIMEOUT_S)


# ---------- 사례 ----------
def load_case(path: Path) -> dict:
    case = json.loads(path.read_text(encoding="utf-8"))
    for task in case["tasks"]:
        task["body"] = json.loads((path.parent / task["request"]).read_text(encoding="utf-8"))
    return case


def entry_key(entry: dict):
    return tuple(entry["source"]) if "source" in entry else ("text", entry["text"])


def material_key(m):
    if not m.sourced:
        return ("text", m.text)
    return (m.agenda_id, m.kind, m.item_id if m.item_id is not None else m.decision_point_id)


def sent_materials(body: dict) -> list:
    """AI 에 가는 재료 (다시 배치면 이전 초안에 없던 요약 항목은 보내지 않으므로 뺀다)."""
    if body.get("previousLines"):
        return build_relayout(body["agendas"], body["summary"], body["previousLines"])[0]
    return build_materials(body["agendas"], body["summary"])


def check_expect(materials, expect) -> list[str]:
    """정답표와 재료가 하나씩 맞물리는지. 틀린 곳의 설명 목록 (빈 목록이면 맞음)."""
    problems, matched = [], set()
    for m in materials:
        key = material_key(m)
        hits = [i for i, e in enumerate(expect) if _matches(e, key)]
        if len(hits) != 1:
            problems.append(f"{m.ref} {key}: 정답표에 {len(hits)}개 ({m.text})")
        matched.update(hits)
    problems += [f"정답표의 {entry_key(e)}: 맞는 재료 없음" for i, e in enumerate(expect) if i not in matched]
    return problems


def _matches(entry, key) -> bool:
    if "source" in entry:
        return tuple(entry["source"]) == key
    return key[0] == "text" and entry["text"] in key[1]


# ---------- 채점 ----------
def line_key(line: dict):
    sources = line.get("sources") or []
    if sources:
        s = sources[0]
        return (s["agendaId"], s["kind"], s["id"] if s["id"] is not None else s["decisionPointId"])
    return ("text", line["sentence"] if "sentence" in line else " | ".join(line.get("cells", [])))


def placed(result: dict) -> list[tuple]:
    """(줄 key, 칸 이름) 목록. 표의 행도 줄로 본다."""
    return [(line_key(line), s["title"]) for s in result.get("sections", []) for line in s.get("evidence", [])]


def score(result: dict, expect: list[dict], tables: list[dict] = ()) -> dict:
    if result["status"] != "SUCCEEDED":
        return {"ok": False, "failed": result["error"]["code"], "wrong": None, "serious": 0, "items": {}, "tables": []}
    where = placed(result)
    items, wrong, serious = {}, 0, 0
    for e in expect:
        got = [title for key, title in where if _matches(e, key)] or [NONE]
        good = len(got) == 1 and got[0] in e["in"]
        wrong += not good
        serious += any(title in e.get("serious", ()) for title in got)
        items[json.dumps(entry_key(e), ensure_ascii=False)] = {"got": got, "ok": good}
    table_scores = [_score_table(result, t) for t in tables]
    ok = wrong == 0 and all(t["ok"] for t in table_scores)
    return {"ok": ok, "failed": None, "wrong": wrong, "serious": serious, "items": items, "tables": table_scores}


def _score_table(result, want):
    sec = next((s for s in result["sections"] if s["title"] == want["field"]), None) or {}
    columns = sec.get("columns") or []
    rows = sum("cells" in line for line in sec.get("evidence", []))
    ok = rows == want.get("rows", rows) and (want.get("columns") is None or len(columns) == want["columns"])
    return {"field": want["field"], "columns": columns, "rows": rows, "ok": ok}


# ---------- 실행 ----------
class Recorder:
    """AI 호출을 감싸 호출마다 종류(배치·다시 배치·표)·시간·토큰·오류를 남긴다."""

    def __init__(self, llm, kinds: dict[str, str]):
        self.llm, self.kinds, self.calls = llm, kinds, []
        self.name, self.model = llm.name, llm.model

    def generate(self, system, user, timeout_s):
        rec = {"kind": self.kinds.get(system, "?"), "error": ""}
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


def variants_of(placement: list[str], relayout: list[str], table: list[str]) -> list[dict]:
    n = max(len(placement), len(relayout), len(table))
    pick = lambda vs, i: vs[min(i, len(vs) - 1)]                                     # noqa: E731
    return [{"placement": pick(placement, i), "relayout": pick(relayout, i), "table": pick(table, i)} for i in range(n)]


def label(v: dict) -> str:
    return f"placement-{v['placement']} relayout-{v['relayout']} table-{v['table']}"


def run_case(case: dict, llm, runs: int, variants: list[dict], out_dir: Path, timeout_s: float = TIMEOUT_S,
             progress=print) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    loaded = [{"placement": load_prompt(v["placement"]), "relayout": load_prompt(v["relayout"], RELAYOUT_KIND),
               "table": load_prompt(v["table"], TABLE_KIND)} for v in variants]
    kinds = {p.text: kind for prompts in loaded for kind, p in prompts.items()}
    rec = Recorder(llm, kinds)
    meta = {"about": case.get("about"), "model": f"{llm.name}/{llm.model}", "runs": runs,
            "variants": [label(v) for v in variants], "startedAt": datetime.now().isoformat(timespec="seconds")}
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    fields = ["variant", "task", "run", "kind", "ms", "input_tokens", "output_tokens", "error"]
    with open(out_dir / "calls.csv", "w", newline="", encoding="utf-8") as f, \
            open(out_dir / "runs.jsonl", "w", encoding="utf-8") as runs_out:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for n in range(1, runs + 1):
            for v, prompts in zip(variants, loaded):          # 버전을 번갈아 돌려 시간대 차이를 고르게
                for task in case["tasks"]:
                    _run_task(task, rec, prompts, label(v), n, timeout_s, writer, runs_out)
                f.flush()
                runs_out.flush()
            progress(f"{n}/{runs} 판 끝")
    return out_dir


def _run_task(task, rec, prompts, variant, n, timeout_s, writer, runs_out):
    kw = {"prompt": prompts["placement"], "relayout_prompt": prompts["relayout"], "table_prompt": prompts["table"]}
    first = _one(task["name"], task["body"], task, rec, kw, variant, n, timeout_s, writer, runs_out)
    then = task.get("then")
    if then and first["status"] == "SUCCEEDED":
        _one(then["name"], relayout_body(task["body"], then, first), then, rec, kw, variant, n, timeout_s, writer,
             runs_out)


def relayout_body(body: dict, then: dict, first: dict) -> dict:
    return dict(body, requestId=f"{body['requestId']}-relayout", template={"items": then["relayoutTo"]},
                previousLines=previous_lines_from(first) + list(then.get("addLines", ())))


def _one(name, body, spec, rec, kw, variant, n, timeout_s, writer, runs_out):
    start = len(rec.calls)
    t0 = time.monotonic()
    result = make_draft(body, rec, timeout_s=timeout_s, **kw)
    calls = rec.calls[start:]
    for c in calls:
        writer.writerow({"variant": variant, "task": name, "run": n, **c})
    runs_out.write(json.dumps({"variant": variant, "task": name, "run": n, "ms": _ms(t0), "calls": calls,
                               "score": score(result, spec["expect"], spec.get("tables", ())), "result": result},
                              ensure_ascii=False) + "\n")
    return result


def _ms(since):
    return round((time.monotonic() - since) * 1000)


# ---------- 요약 ----------
def summarize(out_dir: Path) -> str:
    rows = [json.loads(line) for line in (out_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines() if line]
    groups = {}
    for r in rows:
        groups.setdefault((r["task"], r["variant"]), []).append(r)
    out = ["| 작업 | 지시문 | 맞음 | 틀린 항목 합 | 큰 실수 | 실패 | 호출 | 시간 중앙/최대(초) | 토큰 입력/출력 (판당 평균) |",
           "|---|---|---|---|---|---|---|---|---|"]
    misses = []
    for (task, variant), rs in groups.items():
        ok = sum(r["score"]["ok"] for r in rs)
        wrong = sum(r["score"]["wrong"] or 0 for r in rs)
        serious = sum(r["score"]["serious"] for r in rs)
        failed = sum(bool(r["score"]["failed"]) for r in rs)
        calls = [c for r in rs for c in r["calls"]]
        secs = [r["ms"] / 1000 for r in rs]
        tin = sum(c.get("input_tokens") or 0 for c in calls) / len(rs)
        tout = sum(c.get("output_tokens") or 0 for c in calls) / len(rs)
        out.append(f"| {task} | {variant} | {ok}/{len(rs)} | {wrong} | {serious} | {failed} | {len(calls)} |"
                   f" {statistics.median(secs):.1f} / {max(secs):.1f} | {tin:.0f} / {tout:.0f} |")
        counts = {}
        for r in rs:
            for key, item in r["score"]["items"].items():
                if not item["ok"]:
                    where = " + ".join(item["got"])
                    counts[(key, where)] = counts.get((key, where), 0) + 1
            for t in r["score"]["tables"]:
                if not t["ok"]:
                    k = (f"표 {t['field']}", f"열 {len(t['columns'])}개 행 {t['rows']}개")
                    counts[k] = counts.get(k, 0) + 1
        misses += [f"- {task} / {variant}: {key} → {where} ({c}번)" for (key, where), c in sorted(counts.items())]
    out += ["", "틀린 항목 (어디로 갔는지):", *(misses or ["- 없음"])]
    return "\n".join(out)


# ---------- 실행 진입점 ----------
def dry_run(case: dict) -> bool:
    ok = True
    for task in case["tasks"]:
        materials = sent_materials(task["body"])
        print(f"\n## {task['name']} (재료 {len(materials)}개)")
        good = _print_check(materials, task["expect"])
        ok &= good
        then = task.get("then")
        if then and good:
            ok &= _dry_then(task, materials, then)
    print("\n정답표와 재료가 맞물림. AI 는 부르지 않았다." if ok else "\n정답표를 고쳐야 함.")
    return ok


def _dry_then(task, materials, then) -> bool:
    """처음 초안을 정답 칸(정답이 여럿이면 첫째)에 놓았다고 보고 다시 배치 재료를 만들어 then 의 정답표와 맞춘다."""
    fields, _ = template_fields(task["body"]["template"])
    titles = [f.title for f in fields if f.ai_fill]
    if any(f.type == TABLE for f in fields):
        print(f"\n## {then['name']}: 처음 양식에 표가 있어 점검하지 못함 (실행 때 채점한다)")
        return True
    answer = {"placements": [], "unplaced": []}
    for m in materials:
        title = next(e for e in task["expect"] if _matches(e, material_key(m)))["in"][0]
        if title == NONE:
            answer["unplaced"].append(m.ref)
        else:
            answer["placements"].append({"m": m.ref, "section": titles.index(title) + 1})
    first = make_draft(task["body"], _Fixed(json.dumps(answer, ensure_ascii=False)))
    body = relayout_body(task["body"], then, first)
    print(f"\n## {then['name']} (처음 초안을 정답대로 놓았다고 보고 만든 재료)")
    return _print_check(sent_materials(body), then["expect"])


def _print_check(materials, expect) -> bool:
    for m in materials:
        hit = next((e for e in expect if _matches(e, material_key(m))), None)
        print(f"  {m.ref} {material_key(m)} → {hit['in'] if hit else '정답표에 없음'}  {m.text}")
    problems = check_expect(materials, expect)
    for p in problems:
        print(f"  [문제] {p}")
    return not problems


class _Fixed:
    name, model = "fixed", "fixed"

    def __init__(self, text):
        self.text = text

    def generate(self, system, user, timeout_s):
        return Reply(self.text, None, None)


def main(argv=None):
    p = argparse.ArgumentParser(description="회의록 초안 지시문 평가")
    p.add_argument("case", type=Path, nargs="?", help="사례 파일 (JSON)")
    p.add_argument("--runs", type=int, default=20)
    p.add_argument("--prompt", nargs="+", default=[None], help="배치 지시문 버전 (예: v8 v9). 없으면 가장 큰 번호")
    p.add_argument("--relayout-prompt", nargs="+", default=[None])
    p.add_argument("--table-prompt", nargs="+", default=[None])
    p.add_argument("--timeout", type=float, default=TIMEOUT_S)
    p.add_argument("--dry-run", action="store_true", help="AI 없이 정답표와 재료가 맞물리는지만 본다")
    p.add_argument("--summary", type=Path, metavar="결과폴더", help="저장된 결과로 요약만 다시 낸다")
    args = p.parse_args(argv)
    if args.summary:
        print(summarize(args.summary))
        return
    if not args.case:
        p.error("사례 파일이 필요함")
    case = load_case(args.case)
    if args.dry_run:
        sys.exit(0 if dry_run(case) else 1)
    from .try_draft import real_llm                      # rag/.env 를 읽는다. 키 값은 출력하지 않는다
    llm = real_llm()
    variants = [{k: _resolved(v, kind) for k, v, kind in (("placement", x["placement"], "placement"),
                                                          ("relayout", x["relayout"], RELAYOUT_KIND),
                                                          ("table", x["table"], TABLE_KIND))}
                for x in variants_of(args.prompt, args.relayout_prompt, args.table_prompt)]
    out_dir = OUT_ROOT / f"{args.case.stem}-{datetime.now():%Y%m%d-%H%M%S}"
    print(f"사례: {args.case.name} / AI: {llm.name} ({llm.model}) / {args.runs}판 / " + ", ".join(map(label, variants)),
          flush=True)
    run_case(case, llm, args.runs, variants, out_dir, args.timeout, progress=lambda s: print(s, flush=True))
    print(f"\n결과: {out_dir}\n")
    print(summarize(out_dir))


def _resolved(version, kind):
    """None(가장 큰 번호)을 실제 번호로 바꿔 결과에 남긴다."""
    return load_prompt(version, kind).version.rsplit("-", 1)[1]


if __name__ == "__main__":
    main()
