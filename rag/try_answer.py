"""답변 API 를 손으로 시험하는 대화형 도구. 회의록을 넣고, 처리가 끝나면 질문하고 답을 본다.

서버(터미널 1):
  set -a && . rag/.env && set +a && .venv/bin/uvicorn server.main:app --port 8000
이 도구(터미널 2):
  .venv/bin/python -m rag.try_answer                       합성 예시 회의록으로
  .venv/bin/python -m rag.try_answer --file 회의록.json     임시 형식(summary.json v0.1) 회의록으로
  .venv/bin/python -m rag.try_answer --file a.json --title "첫 회의" --file b.json --title "후속 회의"
                                                            여러 문서를 넣고 문서 전체에서 근거를 찾는다

시험용 워크스페이스(기본 999999)에 넣고, 끝날 때 그 워크스페이스를 지운다 (--keep 이면 남김).
실제 회의 데이터는 레포 밖에 두고 --file 로 경로만 넘긴다.
완료 콜백은 이 도구가 127.0.0.1 의 빈 포트에서 받는다. 서버의 RAG_CALLBACK_ALLOWED_HOSTS 가 비어 있어야 한다.
파이썬 기본 라이브러리만 쓴다.
"""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEMO = Path(__file__).parent / "tests" / "fixtures" / "minutes_demo.json"
PREFIX = "/internal/rag"


class _Callback(BaseHTTPRequestHandler):
    """완료 콜백을 받아 두기만 한다 (서버 로그에 콜백 실패가 찍히지 않게)."""
    received = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            _Callback.received.append(json.loads(self.rfile.read(n) or b"null"))
        except ValueError:
            pass
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def call(server, method, path, body=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(server + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode("utf-8", "replace")


def ingest(server, args, doc_id, title, minutes, callback_url):
    version = str(int(time.time()))                 # 매번 새 버전이라 같은 docId 로 다시 넣어도 다시 처리한다
    t0 = time.monotonic()
    code, body = call(server, "POST", f"{PREFIX}/documents", {
        "docId": doc_id, "docVersion": version, "workspaceId": args.workspace, "sourceType": "minutes",
        "title": title, "minutes": minutes, "callbackUrl": callback_url})
    if code != 202:
        sys.exit(f"  회의록 넣기 실패: {code} {body}")
    while time.monotonic() - t0 < args.wait:
        code, st = call(server, "GET", f"{PREFIX}/documents/{doc_id}")
        if code == 200 and st["docVersion"] == version and st["status"] in ("ready", "failed"):
            if st["status"] == "failed":
                sys.exit(f"  처리 실패: errorCode={st['errorCode']}")
            return st["chunkCount"], time.monotonic() - t0
        time.sleep(0.5)
    sys.exit(f"  {args.wait}초 안에 처리가 끝나지 않음 (서버 로그를 확인)")


def show(result, secs):
    print(f"  ─ 상태: {result['status']}" + (f" / {result['errorCode']}" if result.get("errorCode") else "")
          + f" ({secs:.1f}초)")
    if result.get("answer"):
        print(f"  ─ 답: {result['answer']}")
    for c in result.get("citations", []):
        print(f"  ─ 근거 [{c['ref']}] {c['title']} · {c['location'].get('label', '')} (docId {c['docId']})")
        print(f"         {c['text']}")
    if result["status"] == "no_result":
        print("  ─ (화면에는 \"관련 내용을 찾지 못했습니다\")")


def main(argv=None):
    p = argparse.ArgumentParser(description="회의록을 넣고 답변 API 로 질문해 본다")
    p.add_argument("--file", type=Path, action="append", help="회의록 JSON. 여러 번 쓰면 여러 문서 (생략하면 합성 예시)")
    p.add_argument("--server", default="http://127.0.0.1:8000")
    p.add_argument("--workspace", type=int, default=999999, help="시험용 워크스페이스 ID")
    p.add_argument("--doc-id", type=int, default=900001, help="첫 문서 ID. 다음 문서는 1씩 늘린다")
    p.add_argument("--title", action="append", default=[], help="회의 제목. --file 과 같은 순서로 (생략하면 파일 이름)")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--wait", type=float, default=120, help="처리를 기다리는 최대 초")
    p.add_argument("--keep", action="store_true", help="끝나도 시험 문서를 지우지 않음")
    args = p.parse_args(argv)
    files = args.file or [DEMO]
    if len(args.title) > len(files):
        p.error("--title 이 --file 보다 많음")
    docs = [(args.doc_id + i, (args.title[i] if i < len(args.title) else f.stem), f) for i, f in enumerate(files)]
    doc_ids = [d for d, _, _ in docs]
    server = args.server.rstrip("/")

    print("[1/3] 서버 확인 ...", end=" ", flush=True)
    try:
        call(server, "GET", "/health", timeout=5)
    except OSError:
        sys.exit(f"\n  {server} 에 닿지 않음. 터미널 1에서 서버를 먼저 띄운다 (이 파일 맨 위 설명)")
    print("OK")

    cb = HTTPServer(("127.0.0.1", 0), _Callback)
    threading.Thread(target=cb.serve_forever, daemon=True).start()
    callback_url = f"http://127.0.0.1:{cb.server_port}/callback"

    print(f"[2/3] 회의록 넣기 (워크스페이스 {args.workspace})")
    try:
        for doc_id, title, f in docs:
            print(f"  - {f.name} → docId {doc_id} \"{title}\" ...", end=" ", flush=True)
            minutes = json.loads(f.read_text(encoding="utf-8"))
            chunks, secs = ingest(server, args, doc_id, title, minutes, callback_url)
            print(f"처리 완료, 조각 {chunks}개 ({secs:.1f}초)")
        print(f"[3/3] 질문하세요. 문서 {len(docs)}개 전체에서 근거를 찾습니다 (빈 줄이나 q 를 누르면 끝)\n")
        while True:
            try:
                q = input("  질문> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if q in ("", "q", "quit", "exit"):
                break
            t0 = time.monotonic()
            code, result = call(server, "POST", f"{PREFIX}/answer",
                                {"workspaceId": args.workspace, "docIds": doc_ids, "query": q, "topK": args.top_k})
            if code != 200:
                print(f"  ─ 오류 {code}: {result}\n")
                continue
            show(result, time.monotonic() - t0)
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
