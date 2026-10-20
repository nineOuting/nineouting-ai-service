"""처리 결과를 백엔드로 알리는 완료 콜백.

  POST {callbackUrl}  본문 {docId, docVersion, status, errorCode, chunkCount}
  헤더 X-Request-Id (접수 때 받은 값), 인증 헤더 (auth_headers, 방식이 정해지면 채운다)

재시도 규칙은 server/publisher.py 와 같다 (연동 API 초안 7장 6번 합의).
  - 408·429·5xx·연결 오류만 재시도, 3회, 간격 1초·2초·4초
  - 그 밖의 4xx 는 바로 포기한다

콜백 주소는 요청이 정하므로, 허용한 호스트로만 보낸다 (RAG_CALLBACK_ALLOWED_HOSTS).
로그에는 docId, 상태 코드만 남긴다.
"""
import json
import logging
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

log = logging.getLogger("rag.callback")


class CallbackNotAllowed(ValueError):
    """콜백 주소가 형식에 맞지 않거나 허용 목록에 없다."""


def allowed_hosts_from_env(env):
    """RAG_CALLBACK_ALLOWED_HOSTS="host" 또는 "host:port" 를 쉼표로. 비어 있으면 None (모두 허용, 개발용)."""
    raw = (env.get("RAG_CALLBACK_ALLOWED_HOSTS") or "").strip()
    if not raw:
        return None
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def check_callback_url(url, allowed):
    """허용하지 않는 주소면 CallbackNotAllowed. 오류 메시지에 주소 전체를 넣지 않는다."""
    if not isinstance(url, str) or not url:
        raise CallbackNotAllowed("callbackUrl 이 없음")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CallbackNotAllowed("callbackUrl 은 http 또는 https 주소여야 함")
    if parts.username or parts.password:
        raise CallbackNotAllowed("callbackUrl 에 계정 정보를 넣을 수 없음")
    if allowed is None:
        return
    host = parts.hostname.lower()
    hostport = f"{host}:{parts.port}" if parts.port else host
    if host not in allowed and hostport not in allowed:
        raise CallbackNotAllowed("callbackUrl 의 호스트가 허용 목록에 없음")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 따라가면 허용 목록 밖의 호스트로 갈 수 있어서 따라가지 않는다 (3xx 는 실패로 본다)."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def auth_headers():
    """서버 간 인증 헤더. CN팀 방식이 정해지면 여기만 채운다."""
    return {}


class CallbackSender:
    def __init__(self, allowed=None, timeout=5.0, backoffs=(1.0, 2.0, 4.0), sender=None, sleep=time.sleep):
        self.allowed = allowed
        self.timeout = timeout
        self.backoffs = tuple(backoffs)
        self.sender = sender                   # 시험용: fn(url, body, headers) -> (성공, 재시도 가능, 상태 코드)
        self.sleep = sleep

    def send(self, url, body, request_id=None):
        """보냈으면 True. 끝내 실패하면 False (로그만 남기고 예외는 내지 않는다)."""
        doc_id = body.get("docId")
        try:
            check_callback_url(url, self.allowed)
        except CallbackNotAllowed as e:
            log.error("콜백 거부 doc=%s reason=%s", doc_id, e)
            return False
        headers = {"Content-Type": "application/json", **auth_headers()}
        if request_id:
            headers["X-Request-Id"] = request_id
        for attempt in range(len(self.backoffs) + 1):
            ok, retryable, code = self._send(url, body, headers)
            if ok:
                log.info("콜백 성공 doc=%s status=%s request_id=%s", doc_id, code, request_id)
                return True
            if not retryable or attempt == len(self.backoffs):
                log.error("콜백 실패 doc=%s status=%s request_id=%s", doc_id, code, request_id)
                return False
            log.warning("콜백 재시도 doc=%s status=%s %s번째", doc_id, code, attempt + 1)
            self.sleep(self.backoffs[attempt])
        return False

    def _send(self, url, body, headers):
        if self.sender:
            return self.sender(url, body, headers)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with _OPENER.open(req, timeout=self.timeout) as r:
                return 200 <= r.status < 300, False, r.status
        except urllib.error.HTTPError as e:
            return False, e.code >= 500 or e.code in (408, 429), e.code
        except (urllib.error.URLError, TimeoutError, OSError):
            return False, True, None
