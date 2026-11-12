"""챗봇 API 를 손으로 시험하는 대화형 도구. 회의록을 넣고, 대화하듯 이어서 묻는다. 진행 중 회의도 같이 보낼 수 있다.

서버(터미널 1):
  set -a && . ./.env && set +a && .venv/bin/uvicorn server.main:app --port 8000
이 도구(터미널 2):
  .venv/bin/python -m rag.try_chat                                   합성 예시 회의록으로
  .venv/bin/python -m rag.try_chat --file a.json --title "첫 회의" --file b.json --title "후속 회의"
  .venv/bin/python -m rag.try_chat --show-rewrite                    고친 질문도 보여 준다 (아래)
  .venv/bin/python -m rag.try_chat --project --live rag/tests/fixtures/chat_project/live_1112.json --user 55 --user-name 한지우
      합성 프로젝트의 회의록 5개(10/08~11/05)를 넣고, 11/12 진행 중 회의(안건 보드·최근 전사 줄)를 질문마다 같이 보낸다.
      질문한 사람은 한지우(55). 넘치는 회의는 live_1112_long.json. 첨부 자료(PDF·엑셀)는 S3 주소가 필요해 넣지 않는다

대화 기록은 이 도구가 들고 있다가 질문마다 history 로 보낸다 (실제로는 백엔드가 하는 일, 명세 2장).
명세대로 answered·no_result 답은 기록에 더하고 failed 는 더하지 않는다. 기록은 최근 20개까지 보낸다.
  /new      대화 기록을 비운다 (새 대화)
  /history  지금 대화 기록을 보여 준다
  /live     진행 중 회의 보내기를 끄고 켠다 (--live 를 줬을 때)
  빈 줄, q  끝

--show-rewrite 는 서버와 같은 AI 설정(.env 의 RAG_LLM_*)과 같은 함수로 이 도구가 직접 질문을 고쳐 써 본다.
서버 응답에는 고친 질문이 없어서 이렇게 본다. 질문마다 AI 를 한 번 더 부르고(요금), 서버가 고친 것과 다를 수 있다.
시험용 워크스페이스(기본 999999)에 넣고, 끝날 때 그 워크스페이스를 지운다 (--keep 이면 남김).
실제 회의 데이터는 레포 밖에 두고 --file 로 경로만 넘긴다.
"""
import argparse
import json
import sys
import threading
import time
from http.server import HTTPServer
from pathlib import Path

from .try_answer import DEMO, PREFIX, _Callback, call, ingest

MAX_HISTORY = 20                    # api.MAX_HISTORY 와 같다
PROJECT = Path(__file__).parent / "tests" / "fixtures" / "chat_project"
PROJECT_TITLE = "앱 리뉴얼 주간 회의"   # 평가 사례(chat_eval_project.json)와 같은 제목. 날짜는 회의록의 meetingStartAt 으로 붙는다


def load_live(path: Path) -> dict:
    """진행 중 회의 파일 (make_live_meeting.py 모양): currentAgendaId, liveAgendas, recentLines."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"currentAgendaId": data.get("currentAgendaId"), "liveAgendas": data.get("liveAgendas") or [],
            "recentLines": data.get("recentLines") or []}


class Rewriter:
    """--show-rewrite. 서버와 같은 고쳐 쓰기 함수·지시문·AI 설정."""

    def __init__(self):
        from shared.envfile import local_env
        from shared.llm import LLMConfigError, make_llm

        from .chat import Budget, chat_prompts

        env = local_env()
        try:
            self.llm, _ = make_llm(env, "RAG_LLM")             # 서버처럼 고쳐 쓰기는 기본 모델만 쓴다 (폴백 없음)
        except LLMConfigError as e:
            sys.exit(f"  --show-rewrite AI 설정 오류: {e}")
        self.prompt = chat_prompts(env.get("RAG_CHAT_PROMPT_VERSION"), env.get("RAG_REWRITE_PROMPT_VERSION")).rewrite
        self.timeout_s = float(env.get("RAG_REWRITE_TIMEOUT_S", Budget().rewrite_s))

    def show(self, history, query):
        from .chat import Turn, recent_turns, rewrite_query

        rw = rewrite_query(self.llm, self.prompt, recent_turns([Turn(h["role"], h["text"]) for h in history]),
                           query, self.timeout_s)
        if rw.state == "skip":
            print("  ─ 고친 질문: (대화가 없어 고치지 않음)")
        elif rw.state == "ok":
            print(f"  ─ 고친 질문: {rw.query}" + ("  (그대로)" if rw.query == query else "") + f" ({rw.ms / 1000:.1f}초)")
        else:
            print(f"  ─ 고친 질문: 원래 질문 씀 ({rw.state})")


def show(result, secs):
    print(f"  ─ 상태: {result['status']}" + (f" / {result['errorCode']}" if result.get("errorCode") else "")
          + (f" / 근거 종류 {result['basis']}" if result.get("basis") else "") + f" ({secs:.1f}초)")
    if result.get("answer"):
        print(f"  ─ 답: {result['answer']}")
    for c in result.get("citations", []):
        where = "진행 중 회의" if c["sourceType"] == "live" else f"{c['sourceType']}, docId {c['docId']}"
        print(f"  ─ 근거 [{c['ref']}] {c['title']} · {c['location'].get('label', '')} ({where})")
        print("         " + c["text"].replace("\n", "\n         "))


def main(argv=None):
    p = argparse.ArgumentParser(description="회의록을 넣고 챗봇 API 로 대화하듯 질문해 본다")
    p.add_argument("--file", type=Path, action="append", help="회의록 JSON. 여러 번 쓰면 여러 문서 (생략하면 합성 예시)")
    p.add_argument("--project", action="store_true",
                   help="합성 프로젝트의 회의록 5개(chat_project/meeting_*.json)를 넣는다. --file 과 같이 쓰지 않는다")
    p.add_argument("--live", type=Path, help="진행 중 회의 파일 (예: rag/tests/fixtures/chat_project/live_1112.json)")
    p.add_argument("--no-docs", action="store_true", help="회의록을 넣지 않는다 (진행 중 회의만으로 시험)")
    p.add_argument("--server", default="http://127.0.0.1:8000")
    p.add_argument("--workspace", type=int, default=999999, help="시험용 워크스페이스 ID")
    p.add_argument("--doc-id", type=int, default=900001, help="첫 문서 ID. 다음 문서는 1씩 늘린다")
    p.add_argument("--title", action="append", default=[], help="회의 제목. --file 과 같은 순서로 (생략하면 파일 이름)")
    p.add_argument("--meeting", type=int, default=1, help="요청의 meetingId")
    p.add_argument("--user", type=int, default=1, help="요청의 userId")
    p.add_argument("--user-name", help="요청의 userName (지난 회의록에서 \"내 할 일\"을 찾는 데 쓴다)")
    p.add_argument("--wait", type=float, default=120, help="처리를 기다리는 최대 초")
    p.add_argument("--keep", action="store_true", help="끝나도 시험 문서를 지우지 않음")
    p.add_argument("--show-rewrite", action="store_true", help="고친 질문도 보여 줌 (AI 를 한 번 더 부름)")
    args = p.parse_args(argv)
    if args.project and args.file:
        p.error("--project 와 --file 은 같이 쓰지 않는다")
    if args.no_docs and (args.project or args.file):
        p.error("--no-docs 는 --project·--file 과 같이 쓰지 않는다")
    if args.no_docs and not args.live:
        p.error("--no-docs 는 --live 와 같이 쓴다 (볼 자료가 없음)")
    live = load_live(args.live) if args.live else None
    if args.project:
        files = sorted(PROJECT.glob("meeting_*.json"))
        titles = [PROJECT_TITLE] * len(files)
    else:
        files = [] if args.no_docs else (args.file or [DEMO])
        titles = args.title
    if len(titles) > len(files):
        p.error("--title 이 --file 보다 많음")
    docs = [(args.doc_id + i, (titles[i] if i < len(titles) else f.stem), f) for i, f in enumerate(files)]
    doc_ids = [d for d, _, _ in docs]
    doc_refs = [{"sourceType": "minutes", "docId": d} for d in doc_ids]
    server = args.server.rstrip("/")

    print("[1/3] 서버 확인 ...", end=" ", flush=True)
    try:
        call(server, "GET", "/health", timeout=5)
    except OSError:
        sys.exit(f"\n  {server} 에 닿지 않음. 터미널 1에서 서버를 먼저 띄운다 (이 파일 맨 위 설명)")
    print("OK")
    rewriter = Rewriter() if args.show_rewrite else None

    cb = HTTPServer(("127.0.0.1", 0), _Callback)
    threading.Thread(target=cb.serve_forever, daemon=True).start()
    callback_url = f"http://127.0.0.1:{cb.server_port}/callback"
    history = []
    live_on = live is not None

    print(f"[2/3] 회의록 넣기 (워크스페이스 {args.workspace})")
    try:
        for doc_id, title, f in docs:
            print(f"  - {f.name} → docId {doc_id} \"{title}\" ...", end=" ", flush=True)
            minutes = json.loads(f.read_text(encoding="utf-8"))
            chunks, secs = ingest(server, args, doc_id, title, minutes, callback_url)
            print(f"처리 완료, 조각 {chunks}개 ({secs:.1f}초)")
        if live:
            print(f"  - 진행 중 회의: {args.live.name} (안건 {len(live['liveAgendas'])}개, 전사 줄 {len(live['recentLines'])}줄, "
                  f"지금 안건 {live['currentAgendaId']}). 질문마다 같이 보냄")
        print(f"[3/3] 질문하세요. 앞 대화를 이어받습니다. /new 새 대화, /history 기록 보기"
              + (", /live 진행 중 회의 끄기·켜기" if live else "") + ", 빈 줄이나 q 는 끝\n")
        while True:
            try:
                q = input(f"  질문({len(history) // 2 + 1})> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if q in ("", "q", "quit", "exit"):
                break
            if q == "/new":
                history.clear()
                print("  ─ 대화 기록을 비웠습니다\n")
                continue
            if q == "/history":
                for h in history:
                    print(f"  {'사용자' if h['role'] == 'user' else '챗봇'}: {h['text']}")
                print(f"  ─ {len(history)}개\n")
                continue
            if q == "/live":
                if live is None:
                    print("  ─ --live 로 진행 중 회의 파일을 주지 않았습니다\n")
                else:
                    live_on = not live_on
                    print(f"  ─ 진행 중 회의 보내기: {'켬' if live_on else '끔'}\n")
                continue
            if rewriter:
                rewriter.show(history[-MAX_HISTORY:], q)
            body = {"workspaceId": args.workspace, "meetingId": args.meeting, "userId": args.user,
                    "userName": args.user_name, "query": q, "docs": doc_refs, "history": history[-MAX_HISTORY:]}
            if live_on:
                body.update(live)
            t0 = time.monotonic()
            code, result = call(server, "POST", f"{PREFIX}/chat", body)
            if code != 200:
                print(f"  ─ 오류 {code}: {result}\n")
                continue
            show(result, time.monotonic() - t0)
            if result["status"] in ("answered", "no_result"):
                history += [{"role": "user", "text": q}, {"role": "assistant", "text": result["answer"]}]
            print()
    finally:
        cb.shutdown()
        if args.keep:
            print(f"남겨 둠: 워크스페이스 {args.workspace}, docId {', '.join(map(str, doc_ids))}")
        else:
            code, _ = call(server, "DELETE", f"{PREFIX}/workspaces/{args.workspace}")
            print(f"정리: 워크스페이스 {args.workspace} 의 시험 문서를 지웠습니다" if code == 204
                  else f"정리 실패 ({code}). 직접 지운다: DELETE {PREFIX}/workspaces/{args.workspace}")


if __name__ == "__main__":
    main()
