"""업로드 문서 시험 도구: 가짜 저장소(S3 대신) 서버와 합성 파일. 내용은 모두 지어낸 것이다.
minutes/tests/minutes_fakes.py 와 이름이 겹치지 않게 따로 이름을 붙였다."""
import io
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rag.download import UploadSettings

EXPIRED_BODY = (b'<?xml version="1.0" encoding="UTF-8"?><Error><Code>AccessDenied</Code>'
                b"<Message>Request has expired</Message></Error>")


def pdf_bytes(encrypted=False) -> bytes:
    """형식 판별용 최소 PDF (읽기 시험용 아님. 글 꺼내기는 SCRUM-107 샘플로)."""
    trailer = b"trailer << /Root 1 0 R" + (b" /Encrypt 5 0 R" if encrypted else b"") + b" >>\n%%EOF\n"
    return b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n" + trailer


def office_bytes(kind) -> bytes:
    """안쪽 표시만 있는 워드(docx)·엑셀(xlsx) zip."""
    inner = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml"}[kind]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(inner, "<doc>합성</doc>")
    return buf.getvalue()


def plain_zip() -> bytes:
    """사용자가 묶은 압축 파일 (안에 문서 표시 없음)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("자료/메모.txt", "합성 메모")
    return buf.getvalue()


def zip_bomb(kind="docx", unzipped_mb=50) -> bytes:
    """워드 표시는 있지만 푼 크기가 지나치게 큰 zip (0 으로 채운 큰 파일은 아주 작게 압축된다)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml" if kind == "docx" else "xl/workbook.xml", b"\0" * (unzipped_mb * 1024 * 1024))
    return buf.getvalue()


def ole_bytes(encrypted) -> bytes:
    """OLE 파일 머리 + (암호면) EncryptionInfo 이름. 암호 걸린 오피스 문서·옛 doc·hwp 를 흉내 낸다."""
    body = "EncryptionInfo".encode("utf-16-le") if encrypted else "HwpSummaryInformation".encode("utf-16-le")
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504 + body + b"\0" * 64


class FakeStorage:
    """S3 대신 쓰는 로컬 서버. files[경로] = bytes. 특별한 경로: /expired 403 만료, /forbidden 403, /error 500,
    /slow (응답 전에 멈춤), /redirect 302, /nolength (Content-Length 없이 보냄). 없는 경로는 404."""

    def __init__(self):
        self.files, self.hits = {}, []
        storage = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                storage.hits.append(path)
                if path == "/expired":
                    return self._send(403, EXPIRED_BODY)
                if path == "/forbidden":
                    return self._send(403, b"<Error><Code>AccessDenied</Code></Error>")
                if path == "/error":
                    return self._send(500, b"")
                if path == "/slow":
                    time.sleep(2)
                    return self._send(200, b"%PDF-")
                if path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data")
                    self.end_headers()
                    return None
                if path.startswith("/nolength/"):
                    data = storage.files[path[len("/nolength"):]]
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(data)
                    return None
                if path not in storage.files:
                    return self._send(404, b"")
                return self._send(200, storage.files[path])

            def _send(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://{self.host}{path}?X-Amz-Signature=fake"

    def settings(self, max_bytes=1024 * 1024, timeout_s=1.0):
        return UploadSettings(allowed_hosts=frozenset({self.host}), max_bytes=max_bytes, timeout_s=timeout_s)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
