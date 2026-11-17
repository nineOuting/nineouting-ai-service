"""회의록 양식 가져오기를 손으로 시험하는 도구 (SCRUM-289). 서버는 띄우지 않는다.

  .venv/bin/python -m minutes.try_template_import 양식.pdf                 파일에서 꺼낸 줄만 본다 (모델 호출 없음, 비용 0)
  .venv/bin/python -m minutes.try_template_import 양식.pdf --real          실제 AI 로 항목을 읽는다 (호출 1번 이상, 요금이 나간다)
  .venv/bin/python -m minutes.try_template_import 양식.pdf --real --json   결과 JSON 전체도 출력
  .venv/bin/python -m minutes.try_template_import 양식.pdf --real --show-io  AI 에게 준 글과 원래 답도 출력

먼저 줄 보기(모델 없음)로 PDF 에서 글이 어떻게 나오는지 확인한 뒤 --real 을 쓴다. 표 칸이 줄마다 쪼개져 나오는지,
머리글·바닥글이 줄 위·아래에 있는지가 항목 읽기의 품질을 정한다.
실제 AI 는 서버와 같은 회의록 초안 설정(MINUTES_LLM_*)으로 연결한다 (환경변수가 있으면 그것, 없으면 저장소 루트 .env).
양식 파일은 레포 밖에 두고 경로만 넘긴다 (회사 양식일 수 있다). 출력에는 양식 글이 나오므로 로그나 지라에 붙이지 않는다.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from rag.errors import IngestError
from .template_import import KIND, MAX_CHARS, build_user, import_template, read_lines
from .try_draft import Recorder, real_llm, show_io


class OneFieldFakeLLM:
    """시험용(--real 없이 AI 흐름만 볼 때). 첫 줄을 항목 하나로 답한다."""
    name = "fake"
    model = "first-line"

    def generate(self, system, user, timeout_s, effort=None):
        from shared.llm import Reply
        first = next(l for l in user.splitlines() if l.startswith("L1 "))
        title = first.split(") ", 1)[1]
        return Reply(json.dumps({"header": [], "footer": [], "fields": [
            {"ref": 1, "title": title, "type": "free", "columns": None, "descriptionRefs": []}]}, ensure_ascii=False), None, None)


def show_lines(lines):
    print(f"줄 {len(lines)}개, {len({x.page for x in lines})}쪽, 글 {sum(len(x.text) for x in lines)}자 (상한 {MAX_CHARS}자)\n")
    page = None
    for x in lines:
        if x.page != page:
            page = x.page
            print(f"----- {page}쪽 ({x.page_len}줄)")
        print(f"L{x.ref:<3} {x.text}")


def show_result(res):
    print(f"\n========== 항목 {len(res.fields)}개")
    for i, f in enumerate(res.fields, 1):
        extra = f" 열 {f['columns']}개" if f.get("columns") else ""
        print(f"{i:>2}. [{f['type']}{extra}] {f['title']}" + (f"\n      설명: {f['description']}" if f.get("description") else ""))
    print(f"\n머리글: {res.header!r}\n바닥글: {res.footer!r}")
    for w in res.warnings:
        print(f"경고: {w['code']} — {w['title']}")


def main():
    p = argparse.ArgumentParser(description="회의록 양식 가져오기 손 시험")
    p.add_argument("file", type=Path, help="양식 PDF")
    p.add_argument("--real", action="store_true", help="실제 AI 로 항목을 읽는다 (요금이 나감)")
    p.add_argument("--fake", action="store_true", help="모델 없이 AI 흐름만 (첫 줄을 항목으로 답하는 가짜)")
    p.add_argument("--json", action="store_true", help="결과 JSON 전체도 출력")
    p.add_argument("--show-io", action="store_true", help="AI 호출마다 입력과 원래 답을 출력")
    p.add_argument("--timeout", type=float, default=60.0, help="AI 응답 시간 제한(초)")
    args = p.parse_args()
    try:
        data = args.file.read_bytes()
    except OSError as e:
        sys.exit(f"파일을 읽을 수 없음: {e.strerror}")
    try:
        lines = read_lines(data)
    except IngestError as e:
        sys.exit(f"읽을 수 없는 파일: {e.code}" + (f" ({e.detail})" if e.detail else ""))
    show_lines(lines)
    if not (args.real or args.fake):
        print(f"\nAI 입력 글자 수: {len(build_user(lines))}자. --real 로 항목을 읽어 본다 (지시문: {KIND})")
        return
    llm = Recorder(real_llm() if args.real else OneFieldFakeLLM())
    started = time.monotonic()
    out = import_template(data, llm, args.timeout)
    elapsed = time.monotonic() - started
    if args.show_io:
        show_io(llm.calls)
    print(f"\nAI 호출 {out.attempts}번, {elapsed:.1f}초, 토큰 입력 {out.input_tokens} 출력 {out.output_tokens}")
    if out.error_code:
        sys.exit(f"실패: {out.error_code}")
    show_result(out.result)
    if args.json:
        print(json.dumps({"fields": out.result.fields, "header": out.result.header, "footer": out.result.footer,
                          "warnings": out.result.warnings}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
