"""검색 품질 평가. 질문마다 정답 청크가 검색 결과 몇 위에 오는지 보고 Hit@k·Recall@k·MRR 을 낸다.

실행:
  .venv/bin/python -m rag.eval_search data/eval/questions_v1.json
  .venv/bin/python -m rag.eval_search data/eval/questions_v1.json --k 5 10 20 --detail

질문 파일 (JSON):
  {"docs": {"prep": {"file": "data/a.json", "title": "준비 회의"}, ...},
   "questions": [{"id": 1, "q": "베타 참여자는 몇 명이야?", "type": "사실·숫자",
                  "gold": [{"doc": "prep", "starts": "베타 참여자는 총 100명"}]}, ...]}
  gold 는 찾아와야 할 조각 (그 문서의 조각 중 글이 starts 로 시작하는 것). gold 가 빈 질문은 검색 지표에서 뺀다.

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
from .loaders.minutes import load_minutes


def build_index(docs, embedder):
    """docs: {key: {"file": ..., "title": ...}} → (조각 목록 [(doc, chunk)], 벡터 행렬)."""
    chunks = []
    for key, d in docs.items():
        pieces = load_minutes(json.loads(Path(d["file"]).read_text(encoding="utf-8"))).pieces
        for c in split_pieces(pieces, embedder.passage_tokens, embedder.max_tokens):
            chunks.append((key, c))
    vecs = np.asarray(embedder.embed_passages([c.embed_text for _, c in chunks]), dtype=np.float32)
    return chunks, vecs


def gold_indexes(chunks, gold):
    """gold 항목마다 맞는 조각 번호. 없거나 여럿이면 ValueError (질문 파일이 틀린 것)."""
    out = []
    for g in gold:
        hit = [i for i, (key, c) in enumerate(chunks) if key == g["doc"] and c.content.startswith(g["starts"])]
        if len(hit) != 1:
            raise ValueError(f"정답 조각을 하나로 찾지 못함 ({len(hit)}개): {g['doc']} '{g['starts']}'")
        out.append(hit[0])
    return out


def rank_all(vecs, qvec):
    """유사도 높은 순서의 조각 번호. 벡터는 길이 1로 정규화돼 있어 내적 = 코사인."""
    return list(np.argsort(-(vecs @ qvec), kind="stable"))


def score(order, golds, ks):
    """한 질문의 지표. order: 순위대로 조각 번호, golds: 정답 조각 번호."""
    pos = {idx: r for r, idx in enumerate(order, 1)}
    ranks = sorted(pos[g] for g in golds)
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
    p.add_argument("--k", type=int, nargs="+", default=[5, 10], help="상위 몇 개까지 볼지 (기본 5 10)")
    p.add_argument("--detail", action="store_true", help="질문마다 정답 조각의 순위를 보여 줌")
    p.add_argument("--out", type=Path, help="결과 JSON 을 저장할 곳 (생략하면 화면에만)")
    args = p.parse_args(argv)
    ks = sorted(set(args.k))

    spec = json.loads(args.questions.read_text(encoding="utf-8"))
    from .embedding import E5Embedder
    t0 = time.monotonic()
    embedder = E5Embedder(threads=2)
    chunks, vecs = build_index(spec["docs"], embedder)
    print(f"조각 {len(chunks)}개 (문서 {len(spec['docs'])}개), 모델 {embedder.model_id.split('@')[0]}, "
          f"준비 {time.monotonic() - t0:.1f}초")

    rows, details, skipped = [], [], 0
    for q in spec["questions"]:
        if not q["gold"]:
            skipped += 1
            continue
        golds = gold_indexes(chunks, q["gold"])
        order = rank_all(vecs, np.asarray(embedder.embed_query(q["q"]), dtype=np.float32))
        s = score(order, golds, ks)
        rows.append((q["type"], s))
        details.append({"id": q["id"], "q": q["q"], "type": q["type"], "ranks": s["ranks"],
                        "top": [f"{chunks[i][0]}:{chunks[i][1].content[:30]}" for i in order[:max(ks)]]})

    summary = summarize(rows, ks)
    print(f"질문 {len(rows)}개 평가 (정답이 없는 질문 {skipped}개 제외)\n")
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
