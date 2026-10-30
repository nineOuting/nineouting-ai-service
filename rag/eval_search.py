"""검색 품질 평가. 질문마다 정답 청크가 검색 결과 몇 위에 오는지 보고 Hit@k·Recall@k·MRR 을 낸다.

실행:
  .venv/bin/python -m rag.eval_search data/eval/questions_v1.json
  .venv/bin/python -m rag.eval_search data/eval/questions_v1.json --k 5 10 20 --detail

질문 파일 (JSON):
  {"docs": {"prep": {"file": "data/a.json", "title": "준비 회의"}, ...},
   "questions": [{"id": 1, "q": "베타 참여자는 몇 명이야?", "type": "사실·숫자",
                  "gold": [{"doc": "prep", "starts": "베타 참여자는 총 100명"}]}, ...]}
  gold 는 찾아와야 할 조각 (그 문서의 조각 중 글이 starts 로 시작하는 것). gold 가 빈 질문은 검색 지표에서 뺀다.

업로드 문서(pdf·docx·xlsx)는 정답을 위치로 적는다. 한 문서를 여러 형식으로 두면 --format 으로 고른다:
  {"docs": {"plan": {"pdf": "a.pdf", "docx": "a.docx", "xlsx": "a.xlsx"}, "report": {"file": "b.pdf"}},
   "questions": [{"id": 1, "q": "케이터링 예산은?", "type": "표 속 사실",
                  "gold": [{"doc": "plan", "answer": "6,750,000원", "pdf": {"page": 4},
                            "docx": {"heading": "7. 예산"}, "xlsx": {"sheet": "7. 예산", "row": 4}}]}]}
  위치가 여러 조각으로 나뉘었으면 그중 가장 높은 순위를 쓴다. 고른 형식의 위치가 없는 gold 는 빼고,
  남는 gold 가 없으면 그 질문을 그 형식의 평가에서 뺀다. 예: rag/tests/fixtures/eval_plan.json

조각은 실제 작업자와 같은 함수(로더 → 나누기 → 임베딩)로 만들고, 서버 검색과 같은 모델·같은 기준(코사인)으로 순위를 낸다.
DB 를 쓰지 않고 메모리에서 계산한다. AI(LLM)는 부르지 않는다 (비용 없음, 회의록이 밖으로 나가지 않음).
회의록·질문은 레포 밖(또는 git 이 무시하는 data/)에 두고 경로만 넘긴다. 실제 회의 데이터로 낸 결과는 레포에 올리지 않는다.

  Hit@k     정답 청크가 Top-k 에 하나라도 들어온 질문의 비율
  Recall@k  정답 청크 중 Top-k 에 들어온 비율의 평균 (정답이 여럿인 넓은 질문에서 중요)
  MRR       첫 정답 청크 순위의 역수 평균 (1위 1점, 2위 0.5점, ...)
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from .chunking import split_pieces
from .config import CHUNK_TOKENS
from .loaders.docx import load_docx
from .loaders.minutes import load_minutes
from .loaders.pdf import load_pdf
from .loaders.xlsx import load_xlsx

FORMATS = ("pdf", "docx", "xlsx")
_LOADERS = {".pdf": load_pdf, ".docx": load_docx, ".xlsx": load_xlsx}


def doc_file(d, fmt):
    """문서 항목에서 읽을 파일. 여러 형식으로 둔 문서는 fmt 형식."""
    if "file" in d:
        return Path(d["file"])
    if fmt not in d:
        raise ValueError(f"--format {fmt} 파일이 없는 문서가 있음 (형식: {', '.join(k for k in d if k in FORMATS)})")
    return Path(d[fmt])


def load_pieces(path):
    """작업자와 같은 로더. 회의록은 json, 업로드 문서는 확장자로 고른다."""
    if path.suffix == ".json":
        return load_minutes(json.loads(path.read_text(encoding="utf-8"))).pieces
    return _LOADERS[path.suffix](path.read_bytes()).pieces


def load_chunks(docs, fmt, count_tokens, max_tokens):
    """docs → 조각 목록 [(doc, chunk)]. 작업자와 같은 로더·나누기."""
    return [(key, c) for key, d in docs.items()
            for c in split_pieces(load_pieces(doc_file(d, fmt)), count_tokens, max_tokens)]


def build_index(docs, embedder, fmt=None):
    """docs: {key: {"file": ...} 또는 {"pdf": ..., "docx": ..., "xlsx": ...}} → (조각 목록 [(doc, chunk)], 벡터 행렬)."""
    chunks = load_chunks(docs, fmt, embedder.passage_tokens, min(CHUNK_TOKENS, embedder.max_tokens))
    vecs = np.asarray(embedder.embed_passages([c.embed_text for _, c in chunks]), dtype=np.float32)
    return chunks, vecs


def at_location(location, want):
    """조각 위치가 정답 위치에 맞나. row 는 조각의 행 범위 안이면 맞다."""
    if "row" in want and not (location.get("rows") and location["rows"][0] <= want["row"] <= location["rows"][1]):
        return False
    return all(location.get(k) == v for k, v in want.items() if k != "row")


def gold_for(gold, fmt):
    """고른 형식으로 채점할 수 있는 gold 만. starts 로 적은 것(회의록)은 형식과 관계없다."""
    return [g for g in gold if "starts" in g or (fmt and fmt in g)]


def gold_indexes(chunks, gold, fmt=None):
    """gold 항목마다 맞는 조각 번호 목록. 없으면(starts 는 여럿이어도) ValueError (질문 파일이 틀린 것)."""
    out = []
    for g in gold:
        if "starts" in g:
            hit = [i for i, (key, c) in enumerate(chunks) if key == g["doc"] and c.content.startswith(g["starts"])]
            if len(hit) != 1:
                raise ValueError(f"정답 조각을 하나로 찾지 못함 ({len(hit)}개): {g['doc']} '{g['starts']}'")
        else:
            hit = [i for i, (key, c) in enumerate(chunks) if key == g["doc"] and at_location(c.location, g[fmt])]
            if not hit:
                raise ValueError(f"정답 위치의 조각이 없음: {g['doc']} {g[fmt]}")
        out.append(hit)
    return out


def rank_all(vecs, qvec):
    """유사도 높은 순서의 조각 번호. 벡터는 길이 1로 정규화돼 있어 내적 = 코사인."""
    return list(np.argsort(-(vecs @ qvec), kind="stable"))


def score(order, golds, ks):
    """한 질문의 지표. order: 순위대로 조각 번호, golds: 정답마다 조각 번호 목록 (가장 높은 순위를 쓴다)."""
    pos = {idx: r for r, idx in enumerate(order, 1)}
    ranks = sorted(min(pos[i] for i in g) for g in golds)
    out = {"ranks": ranks, "mrr": 1 / ranks[0]}
    for k in ks:
        inside = sum(r <= k for r in ranks)
        out[f"hit@{k}"] = float(inside > 0)
        out[f"recall@{k}"] = inside / len(ranks)
    return out


def summarize(rows, ks):
    """유형별·전체 평균. rows: [(type, score dict)]."""
    groups = defaultdict(list)
    for t, s in rows:
        groups[t].append(s)
        groups["전체"].append(s)
    keys = [f"hit@{k}" for k in ks] + [f"recall@{k}" for k in ks] + ["mrr"]
    return {t: {"n": len(ss), **{k: sum(s[k] for s in ss) / len(ss) for k in keys}} for t, ss in groups.items()}


def where(chunk):
    """결과에 보일 조각 표시. 회의록은 글 앞부분, 업로드 문서는 위치 label."""
    return chunk.content[:30] if chunk.location.get("type") == "time" else chunk.location["label"]


def table(summary, ks):
    head = ["유형", "질문"] + [f"Hit@{k}" for k in ks] + [f"Recall@{k}" for k in ks] + ["MRR"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for t, s in sorted(summary.items(), key=lambda kv: (kv[0] == "전체", kv[0])):
        cells = [t, str(s["n"])] + [f"{s[f'hit@{k}']:.0%}" for k in ks] + [f"{s[f'recall@{k}']:.0%}" for k in ks]
        lines.append("| " + " | ".join(cells + [f"{s['mrr']:.2f}"]) + " |")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="검색 품질 평가 (AI 를 부르지 않음)")
    p.add_argument("questions", type=Path, help="질문 파일 (JSON)")
    p.add_argument("--format", choices=FORMATS, help="여러 형식으로 둔 문서를 어느 형식으로 읽을지")
    p.add_argument("--k", type=int, nargs="+", default=[5, 10], help="상위 몇 개까지 볼지 (기본 5 10)")
    p.add_argument("--detail", action="store_true", help="질문마다 정답 조각의 순위를 보여 줌")
    p.add_argument("--out", type=Path, help="결과 JSON 을 저장할 곳 (생략하면 화면에만)")
    args = p.parse_args(argv)
    ks = sorted(set(args.k))

    spec = json.loads(args.questions.read_text(encoding="utf-8"))
    from .embedding import E5Embedder
    t0 = time.monotonic()
    embedder = E5Embedder(threads=2)
    chunks, vecs = build_index(spec["docs"], embedder, args.format)
    print(f"조각 {len(chunks)}개 (문서 {len(spec['docs'])}개{', ' + args.format if args.format else ''}), "
          f"모델 {embedder.model_id.split('@')[0]}, 준비 {time.monotonic() - t0:.1f}초")

    rows, details, skipped = [], [], 0
    for q in spec["questions"]:
        gold = gold_for(q["gold"], args.format)
        if not gold:
            skipped += 1
            continue
        golds = gold_indexes(chunks, gold, args.format)
        order = rank_all(vecs, np.asarray(embedder.embed_query(q["q"]), dtype=np.float32))
        s = score(order, golds, ks)
        rows.append((q["type"], s))
        details.append({"id": q["id"], "q": q["q"], "type": q["type"], "ranks": s["ranks"],
                        "top": [f"{chunks[i][0]}:{where(chunks[i][1])}" for i in order[:max(ks)]]})

    summary = summarize(rows, ks)
    print(f"질문 {len(rows)}개 평가 (정답이 없거나 이 형식에 정답 위치가 없는 질문 {skipped}개 제외)\n")
    print(table(summary, ks))
    if args.detail:
        print("\n| # | 질문 | 유형 | 정답 조각 순위 |\n|---|---|---|---|")
        for d in details:
            print(f"| {d['id']} | {d['q']} | {d['type']} | {', '.join(map(str, d['ranks']))} |")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"model": embedder.model_id, "k": ks, "summary": summary, "questions": details},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n저장: {args.out}")


if __name__ == "__main__":
    sys.exit(main())
