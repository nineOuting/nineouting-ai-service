"""초안 요청을 N배로 불려 긴 회의처럼 만든다 (손으로 시험하는 도구. 모델을 부르지 않는다).

요청의 안건과 요약을 N번 복사해서 재료 수를 늘린다. 안건 번호, 결정할 사항 번호, 항목 번호는 겹치지 않게 바꾸고 안건 제목에는 (2), (3)을 붙인다.
근거 줄 번호와 글은 그대로라서 복사본은 같은 내용이다. 모델이 복사본을 합치지는 않지만 실제 긴 회의보다 내용이 단조로워
오인식 교정처럼 "찾은 것" 수에 달린 호출은 실제보다 가벼울 수 있다. 재료 수에 비례하는 호출(칸 배치, 문장 다듬기)의 시간을 보는 용도다.

  python -m minutes.scale_request minutes/tests/fixtures/eval_long_request.json data/eval_v2/req_long_x3.json --times 3
  time python -m minutes.try_draft --file data/eval_v2/req_long_x3.json --real          # 실제 AI. 호출마다 요금이 나간다

출력 파일에는 요약 내용이 들어 있을 수 있으니 커밋하지 않는 폴더(data/)에 둔다.
"""
import argparse
import copy
import json
import sys
from pathlib import Path

from .materials import build_materials
from .request import InvalidInput, validate_request

OFFSET = 100_000          # 복사본마다 정수 번호에 더하는 값 (원래 번호가 이보다 작아야 겹치지 않는다)


def _shift(value, k):
    """번호를 복사본 k(0부터)에 맞게 바꾼다. 정수는 더하고 글은 꼬리표를 붙인다. None 은 그대로."""
    if value is None or k == 0:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value + k * OFFSET
    return f"{value}-c{k}"


def scale(body: dict, times: int) -> dict:
    out = copy.deepcopy(body)
    agendas, summary_agendas = [], []
    for k in range(times):
        for a in body["agendas"]:
            a2 = copy.deepcopy(a)
            a2["agendaId"] = _shift(a["agendaId"], k)
            if k:
                a2["title"] = f"{a['title']} ({k + 1})"
            for p in a2.get("decisionPoints") or []:
                p["decisionPointId"] = _shift(p["decisionPointId"], k)
            agendas.append(a2)
        for a in body["summary"]["agendas"]:
            a2 = copy.deepcopy(a)
            a2["agendaId"] = _shift(a["agendaId"], k)
            for p in a2.get("decisionPoints") or []:
                p["decisionPointId"] = _shift(p.get("decisionPointId"), k)
            for key in ("summary", "opinions", "todos"):
                for item in a2.get(key) or []:
                    item["id"] = _shift(item.get("id"), k)
            summary_agendas.append(a2)
    out["agendas"] = agendas
    out["summary"] = dict(body["summary"], agendas=summary_agendas)
    out["requestId"] = f"{body['requestId']}-x{times}"
    return out


def main():
    p = argparse.ArgumentParser(description="초안 요청을 N배로 불려 긴 회의처럼 만든다")
    p.add_argument("source", type=Path, help="원래 요청 JSON")
    p.add_argument("target", type=Path, help="저장할 경로 (data/ 아래 권장)")
    p.add_argument("--times", type=int, default=3, help="몇 배로 불릴지")
    args = p.parse_args()
    if args.times < 1:
        sys.exit("--times 는 1 이상이어야 합니다")
    body = json.loads(args.source.read_text(encoding="utf-8"))
    out = scale(body, args.times)
    try:
        validate_request(out)
    except InvalidInput as e:
        sys.exit(f"불린 요청이 형식 검사에 걸림: {e}")
    before, after = len(build_materials(body["agendas"], body["summary"])), len(build_materials(out["agendas"], out["summary"]))
    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"재료 {before}개 -> {after}개, 안건 {len(body['agendas'])}개 -> {len(out['agendas'])}개 ({args.target})")


if __name__ == "__main__":
    main()
