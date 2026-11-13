"""챗봇 API 를 손으로 시험하는 대화형 도구. 회의록을 넣고, 대화하듯 이어서 묻는다. 진행 중 회의도 같이 보낼 수 있다.

서버(터미널 1):
  set -a && . ./.env && set +a && .venv/bin/uvicorn server.main:app --port 8000
이 도구(터미널 2):
  .venv/bin/python -m rag.try_chat                                   합성 예시 회의록으로
  .venv/bin/python -m rag.try_chat --file a.json --title "첫 회의" --file b.json --title "후속 회의"
  .venv/bin/python -m rag.try_chat --show-rewrite                    고친 질문도 보여 준다 (아래)
  .venv/bin/python -m rag.try_chat --project --live rag/tests/fixtures/chat_project/live_1112.json --user 55 --user-name 한지우
      합성 프로젝트의 회의록 5개(10/08~11/05)를 넣고, 11/12 진행 중 회의(안건 보드·최근 전사 줄)를 질문마다 같이 보낸다.
      질문한 사람은 한지우(55). 넘치는 회의는 live_1112_long.json.
      --project 는 첨부 자료 3개(요구사항 정의서 PDF, 일정·예산 엑셀, 관계없는 행사 계획서 PDF)도 넣는다. 이 도구가
      127.0.0.1:8795 에 파일 서버를 띄우고 그 주소를 fileUrl 로 보내므로, 서버를 이렇게 띄워야 한다 (.env 는 고치지 않음):
        set -a && . ./.env && set +a && RAG_UPLOAD_ALLOWED_HOSTS=127.0.0.1:8795 .venv/bin/uvicorn server.main:app --port 8000
      서버가 허용하지 않으면 첨부 자료는 건너뛰고 회의록만으로 이어 간다 (--no-uploads 면 처음부터 넣지 않음).
      첨부 자료의 "올린 날"은 서버가 받은 시각(오늘)이다. 회의록보다 나중 날짜로 보인다

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
import hashlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .try_answer import DEMO, PREFIX, _Callback, call, ingest

MAX_HISTORY = 20                    # api.MAX_HISTORY 와 같다
PROJECT = Path(__file__).parent / "tests" / "fixtures" / "chat_project"
PROJECT_TITLE = "앱 리뉴얼 주간 회의"   # 평가 사례(chat_eval_project.json)와 같은 제목. 날짜는 회의록의 meetingStartAt 으로 붙는다
PROJECT_UPLOADS = [                    # (파일, 제목). 평가 사례와 같은 제목
    (PROJECT / "requirements.pdf", "쇼핑 앱 2.0 요구사항 정의서 v1.0.pdf"),
    (PROJECT / "schedule_budget.xlsx", "일정_예산.xlsx"),
    (PROJECT.parent / "upload_plan.pdf", "고객 초청 기술 세미나 운영계획서.pdf"),
]
FILE_PORT = 8795


class _Files(BaseHTTPRequestHandler):
    """첨부 자료를 내주는 작은 파일 서버 (S3 대신). 등록한 경로만 내준다."""
    files: dict[str, bytes] = {}

    def do_GET(self):
        data = self.files.get(self.path.split("?")[0])
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def ingest_upload(server, args, doc_id, title, file_url, version, callback_url):
    """첨부 자료 넣기. (조각 수, 걸린 초) 또는 (None, 이유). 서버가 거절하면 끝내지 않고 이유를 돌려준다."""
    t0 = time.monotonic()
    code, body = call(server, "POST", f"{PREFIX}/documents", {
        "docId": doc_id, "docVersion": version, "workspaceId": args.workspace, "sourceType": "upload",
        "title": title, "fileUrl": file_url, "callbackUrl": callback_url})
    if code != 202:
        return None, f"거절 {code}: {body.get('detail') if isinstance(body, dict) else body}"
    while time.monotonic() - t0 < args.wait:
        code, st = call(server, "GET", f"{PREFIX}/documents/upload/{doc_id}")
        if code == 200 and st["docVersion"] == version and st["status"] in ("ready", "failed"):
            if st["status"] == "failed":
                return None, f"처리 실패 errorCode={st['errorCode']}"
            return st["chunkCount"], time.monotonic() - t0
        time.sleep(0.5)
    return None, f"{args.wait}초 안에 처리가 끝나지 않음 (서버 로그를 확인)"


def load_live(path: Path) -> dict:
    """진행 중 회의 파일 (make_live_meeting.py 모양): currentAgendaId, liveAgendas, recentLines, liveSummary(있으면)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"currentAgendaId": data.get("currentAgendaId"), "liveAgendas": data.get("liveAgendas") or [],
            "recentLines": data.get("recentLines") or [], "liveSummary": data.get("liveSummary") or []}


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
    p.add_argument("--no-uploads", action="store_true", help="--project 에서 첨부 자료를 넣지 않는다")
    p.add_argument("--file-port", type=int, default=FILE_PORT,
                   help="첨부 자료 파일 서버 포트. 서버의 RAG_UPLOAD_ALLOWED_HOSTS 에 127.0.0.1:이 포트가 있어야 한다")
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
    # 첨부 자료는 회의록과 같은 번호를 쓴다 (sourceType 이 달라 다른 문서다, 명세 3.2)
    uploads = [] if not args.project or args.no_uploads else \
        [(args.doc_id + i, title, f) for i, (f, title) in enumerate(PROJECT_UPLOADS)]
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
    fs = None
    if uploads:
        try:
            fs = HTTPServer(("127.0.0.1", args.file_port), _Files)
        except OSError:
            sys.exit(f"  첨부 자료 파일 서버를 127.0.0.1:{args.file_port} 에 띄우지 못함 (포트를 쓰는 중). --file-port 로 바꾼다")
        threading.Thread(target=fs.serve_forever, daemon=True).start()
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
        if uploads:
            print(f"  첨부 자료 (파일 서버 127.0.0.1:{args.file_port})")
        for doc_id, title, f in uploads:
            data = f.read_bytes()
            _Files.files[f"/{f.name}"] = data
            version = "sha256:" + hashlib.sha256(data).hexdigest()[:16]
            print(f"  - {f.name} → upload {doc_id} \"{title}\" ...", end=" ", flush=True)
            chunks, info = ingest_upload(server, args, doc_id, title, f"http://127.0.0.1:{args.file_port}/{f.name}",
                                         version, callback_url)
            if chunks is None:
                print(info)
                if "허용" in info or "설정" in info:
                    print(f"    서버에 RAG_UPLOAD_ALLOWED_HOSTS=127.0.0.1:{args.file_port} 를 주고 띄워야 한다 (이 파일 맨 위 설명). "
                          "첨부 자료 없이 이어 간다")
                    break
                continue
            print(f"처리 완료, 조각 {chunks}개 ({info:.1f}초)")
            doc_refs.append({"sourceType": "upload", "docId": doc_id})
        if live:
            print(f"  - 진행 중 회의: {args.live.name} (안건 {len(live['liveAgendas'])}개, 전사 줄 {len(live['recentLines'])}줄, 실시간 요약 {len(live['liveSummary'])}개, "
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
        if fs:
            fs.shutdown()
        if args.keep:
            print(f"남겨 둠: 워크스페이스 {args.workspace}, docId {', '.join(map(str, doc_ids))}")
        else:
            code, _ = call(server, "DELETE", f"{PREFIX}/workspaces/{args.workspace}")
            print(f"정리: 워크스페이스 {args.workspace} 의 시험 문서를 지웠습니다" if code == 204
                  else f"정리 실패 ({code}). 직접 지운다: DELETE {PREFIX}/workspaces/{args.workspace}")


if __name__ == "__main__":
    main()
