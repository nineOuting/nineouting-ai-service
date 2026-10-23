"""회의록 초안을 손으로 시험하는 도구. 요청 JSON 을 넣고 칸별 글을 본다. 서버는 띄우지 않는다.

  .venv/bin/python -m minutes.try_draft                          합성 요청, 가짜 AI
  .venv/bin/python -m minutes.try_draft --real                   합성 요청, 실제 AI (rag/.env 의 RAG_LLM_* 와 API 키)
  .venv/bin/python -m minutes.try_draft --file 요청.json --real   다른 요청 파일
  .venv/bin/python -m minutes.try_draft --real --json            결과 JSON 전체도 출력

가짜 AI 는 모든 재료를 첫 칸에 넣고 글을 원래 글 그대로 돌려준다. 흐름이 도는지만 본다.
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

from rag.llm import LLMConfigError, Reply, make_llm
from rag.try_answer import load_env_file

from .draft import make_draft

SAMPLE = Path(__file__).parent / "tests" / "fixtures" / "draft_request.json"
ENV_FILE = Path(__file__).resolve().parent.parent / "rag" / ".env"
_MATERIAL = re.compile(r"^(m\d+) \[.*?\] \((다듬기|고정)\) (.*)$", re.M)


class PlaceAllFakeLLM:
    """시험용. 모든 재료를 1번 칸에 넣고, 다듬기 재료의 글은 원래 글 그대로 돌려준다."""
    name = "fake"
    model = "place-all"

    def generate(self, system, user, timeout_s):
        placements = []
        for ref, how, text in _MATERIAL.findall(user):
            p = {"m": ref, "section": 1}
            if how == "다듬기":
                p["text"] = text
            placements.append(p)
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
        llm, _ = make_llm(env)                     # 시간 제한은 이 도구의 --timeout 을 쓴다
    except LLMConfigError as e:
        sys.exit(f"실제 AI 설정 오류: {e}")
    if llm.name == "fake":
        sys.exit("RAG_LLM_PROVIDER 가 fake 다. rag/.env 에 gemini 나 openai 를 넣어야 실제 AI 로 돈다")
    return llm


def show(result):
    if result["status"] != "SUCCEEDED":
        print(f"\n실패: {result['error']['code']} — {result['error']['message']}")
        return
    for s in result["sections"]:
        print(f"\n■ {s['title']}\n{s['content']}")
    print("\n■ 넣지 못한 재료")
    print("\n".join(f"- 안건 {u['agendaId']} / {u['kind']} / {u['id']}" for u in result["unplaced"]) or "없음")


def main():
    p = argparse.ArgumentParser(description="회의록 초안을 만들어 칸별 글을 본다")
    p.add_argument("--file", type=Path, default=SAMPLE, help="요청 JSON (생략하면 합성 요청)")
    p.add_argument("--real", action="store_true", help="실제 AI 로 (rag/.env 설정)")
    p.add_argument("--timeout", type=float, default=60.0, help="AI 응답을 기다리는 초")
    p.add_argument("--retries", type=int, default=2, help="검사에 걸렸을 때 다시 시키는 횟수")
    p.add_argument("--json", action="store_true", help="결과 JSON 전체도 출력")
    p.add_argument("--show-io", action="store_true", help="AI 호출마다 지시문·입력·원래 답·토큰을 출력")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="[로그] %(message)s")
    body = json.loads(args.file.read_text(encoding="utf-8"))
    llm = Recorder(real_llm() if args.real else PlaceAllFakeLLM())
    print(f"요청: {args.file.name} / AI: {llm.name} ({llm.model})", flush=True)
    try:
        result = make_draft(body, llm, timeout_s=args.timeout, max_retries=args.retries)
    except (KeyError, TypeError) as e:          # 요청 검사(request.py)를 붙이기 전까지
        sys.exit(f"요청 형식이 맞지 않음: {type(e).__name__} {e}")
    if args.show_io:
        show_io(llm.calls)
    show(result)
    if args.json:
        print("\n" + json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
