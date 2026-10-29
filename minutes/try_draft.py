"""회의록 초안을 손으로 시험하는 도구. 요청 JSON 을 넣고 칸별 글을 본다. 서버는 띄우지 않는다.

  .venv/bin/python -m minutes.try_draft                          합성 요청, 가짜 AI
  .venv/bin/python -m minutes.try_draft --real                   합성 요청, 실제 AI (MINUTES_LLM_* 없으면 RAG_LLM_*, 그리고 API 키)
  .venv/bin/python -m minutes.try_draft --file 요청.json --real   다른 요청 파일
  .venv/bin/python -m minutes.try_draft --real --json            결과 JSON 전체도 출력
  .venv/bin/python -m minutes.try_draft --file minutes/tests/fixtures/relayout_request.json --real
                                                                 양식 변경 시 다시 배치 (합성 예시, 고친 줄·새 줄 포함)
  .venv/bin/python -m minutes.try_draft --file minutes/tests/fixtures/table_request.json --real
                                                                 AI 가 채우는 표 필드 (할 일 표, 열 4개)
  .venv/bin/python -m minutes.try_draft --real --relayout-to "결정 사항,할 일,고객 요청"
                                                                 처음 초안 → 그 줄을 새 양식으로 다시 배치 (AI 2번)

가짜 AI 는 모든 재료를 첫 칸에 넣는다. 흐름이 도는지만 본다.
실제 AI 는 RAG 답변과 같은 설정으로 연결한다 (환경변수가 있으면 그것, 없으면 rag/.env). 키 값은 출력하지 않는다.
실제 회의 데이터는 레포 밖에 두고 --file 로 경로만 넘긴다.
"""
import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from rag.llm import LLMConfigError, Reply
from rag.try_answer import load_env_file

from .draft import make_draft
from .llm_config import make_minutes_llm
from .relayout import previous_lines_from
from .request import InvalidInput, validate_request

SAMPLE = Path(__file__).parent / "tests" / "fixtures" / "draft_request.json"
ENV_FILE = Path(__file__).resolve().parent.parent / "rag" / ".env"
_MATERIAL = re.compile(r"^(m\d+) \[", re.M)


class PlaceAllFakeLLM:
    """시험용. 모든 재료를 1번 칸에 넣는다."""
    name = "fake"
    model = "place-all"

    def generate(self, system, user, timeout_s):
        placements = [{"m": ref, "section": 1} for ref in _MATERIAL.findall(user)]
        return Reply(json.dumps({"placements": placements, "unplaced": []}, ensure_ascii=False), None, None)


class Recorder:
    """AI 호출을 감싸 지시문·입력·원래 답·토큰을 기록한다 (--show-io)."""

    def __init__(self, llm):
        self.llm, self.calls = llm, []
        self.name, self.model = llm.name, llm.model

    def generate(self, system, user, timeout_s):
        reply = self.llm.generate(system, user, timeout_s)
        self.calls.append((system, user, reply))
        return reply


def show_io(calls):
    for i, (system, user, reply) in enumerate(calls, 1):
        print(f"\n========== AI 호출 {i}번 ==========")
        if i == 1:
            print(f"--- 지시문 (system)\n{system}")
        print(f"--- 입력 (user)\n{user}")
        print(f"--- AI 답 (원래 글)\n{reply.text}")
        print(f"--- 토큰: 입력 {reply.input_tokens}, 출력 {reply.output_tokens}")


def real_llm():
    env = {**load_env_file(ENV_FILE), **os.environ}
    try:
        llm, _, source = make_minutes_llm(env)     # 시간 제한은 이 도구의 --timeout 을 쓴다
    except LLMConfigError as e:
        sys.exit(f"실제 AI 설정 오류: {e}")
    if llm.name == "fake":
        sys.exit(f"{source}_PROVIDER 가 fake 다. rag/.env 에 gemini 나 openai 를 넣어야 실제 AI 로 돈다")
    return llm


def show(result, previous=None, before_items=None):
    """결과 출력. previous(다시 배치의 previousLines)가 있으면 바꾸기 전 초안을 먼저 보여 주고,
    바꾼 뒤의 줄마다 이전 초안에서 있던 칸을 붙인다. before_items(처음 양식)를 모르면 줄이 있는 칸만 보인다."""
    if previous:
        titles = before_items or list(dict.fromkeys(line["title"] for line in previous))
        note = "" if before_items else ", 줄이 있는 칸만"
        print(f"\n========== 바꾸기 전 초안 ({' / '.join(titles)}{note})")
        for title in titles:
            print(f"■ {title}")
            print("\n".join(f"- {_mark(l)}{l['sentence']}" for l in previous if l["title"] == title)
                  or "해당 내용 없음")
    if result["status"] != "SUCCEEDED":
        print(f"\n실패: {result['error']['code']} — {result['error']['message']}")
        return
    if previous:
        print(f"\n========== 바꾼 뒤 ({' / '.join(s['title'] for s in result['sections'])})")
    where = _previous_titles(result, previous or [])
    for s in result["sections"]:
        if not previous:
            print(f"\n■ {s['title']}\n{s['content']}")
            continue
        print(f"■ {s['title']}")
        print("\n".join(f"- {_mark(line)}{_text(line)}{where[id(line)]}" for line in s["evidence"])
              or "해당 내용 없음")
    print("\n■ 넣지 못한 재료")
    print("\n".join(f"- 안건 {u['agendaId']} / {u['kind']} / {u['id']}" for u in result["unplaced"]) or "없음")
    if previous:
        print("\n■ 넣지 못한 이전 줄 (이전 칸 → 글)")
        print("\n".join(f"- {_mark(line)}{line['title']} → {line['sentence']}" for line in result["unplacedLines"])
              or "없음")


def _text(line) -> str:
    """줄은 글, 표의 행은 칸을 | 로 이은 글."""
    return line["sentence"] if "sentence" in line else "| " + " | ".join(line["cells"]) + " |"


def _mark(line) -> str:
    if not line.get("draftEdited"):
        return ""
    return "[고침] " if line.get("sources") else "[새 줄] "


def _previous_titles(result, previous) -> dict:
    """바꾼 뒤의 줄 → "   ← 이전: {칸}". 글이 같은 이전 줄을 먼저 찾고, 없으면 출처가 같은 줄
    (고치지 않은 줄은 요약에서 다시 만들므로, 요약을 점검에서 고쳤으면 글이 바뀔 수 있다)."""
    unused, out = list(previous), {}
    lines = [line for s in result["sections"] for line in s["evidence"]]
    for line in lines:                                    # 글이 같은 줄
        hit = next((p for p in unused if p["sentence"] == line.get("sentence")), None)
        if hit:
            unused.remove(hit)
            out[id(line)] = f"   ← 이전: {hit['title']}"
    for line in lines:                                    # 글이 바뀐 줄은 출처로
        if id(line) in out:
            continue
        hit = next((p for p in unused if line["sources"] and p["sources"][:1] == line["sources"]), None)
        if hit:
            unused.remove(hit)
        out[id(line)] = f"   ← 이전: {hit['title']} (요약에서 다시 만듦)" if hit else ""
    return out


def run(body, llm, args, previous=None, before_items=None):
    try:
        validate_request(body)
    except InvalidInput as e:
        sys.exit(f"요청 형식 오류 (API 였다면 400): {e}")
    start = len(llm.calls)
    result = make_draft(body, llm, timeout_s=args.timeout, max_retries=args.retries)
    if args.show_io:
        show_io(llm.calls[start:])
    show(result, previous, before_items)
    if args.json:
        print("\n" + json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    p = argparse.ArgumentParser(description="회의록 초안을 만들어 칸별 글을 본다")
    p.add_argument("--file", type=Path, default=SAMPLE, help="요청 JSON (생략하면 합성 요청)")
    p.add_argument("--real", action="store_true", help="실제 AI 로 (rag/.env 설정)")
    p.add_argument("--timeout", type=float, default=60.0, help="AI 응답을 기다리는 초")
    p.add_argument("--retries", type=int, default=2, help="검사에 걸렸을 때 다시 시키는 횟수")
    p.add_argument("--json", action="store_true", help="결과 JSON 전체도 출력")
    p.add_argument("--show-io", action="store_true", help="AI 호출마다 지시문·입력·원래 답·토큰을 출력")
    p.add_argument("--relayout-to", metavar="칸1,칸2", help="처음 초안을 만든 뒤 그 줄을 이 양식으로 다시 배치한다")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="[로그] %(message)s")
    body = json.loads(args.file.read_text(encoding="utf-8"))
    llm = Recorder(real_llm() if args.real else PlaceAllFakeLLM())
    print(f"요청: {args.file.name} / AI: {llm.name} ({llm.model})", flush=True)
    if not args.relayout_to:
        run(body, llm, args, body.get("previousLines"))
        return
    if body.get("previousLines"):
        sys.exit("--relayout-to 는 처음 만들기 요청(previousLines 없음)에만 쓴다")
    items = [name.strip() for name in args.relayout_to.split(",") if name.strip()]
    print(f"\n########## 1. 처음 초안 ({' / '.join(body['template']['items'])})")
    first = run(body, llm, args)
    if first["status"] != "SUCCEEDED":
        return
    print(f"\n########## 2. 양식을 바꿔 다시 배치")
    second = dict(body, requestId=f"{body['requestId']}-relayout", template={"items": items},
                  previousLines=previous_lines_from(first))
    run(second, llm, args, second["previousLines"], body["template"]["items"])


if __name__ == "__main__":
    main()
