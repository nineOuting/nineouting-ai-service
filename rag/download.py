"""업로드 문서 받기 (S3 서명 주소). RAG 명세 "업로드 문서", SCRUM-106.

  check_file_url()  접수할 때: 허용한 저장소 주소인지. 아니면 FileUrlNotAllowed (API 가 400 으로 바꾼다)
  fetch()           처리할 때: 파일을 메모리로 받는다. 실패하면 IngestError
                      S3 가 "Request has expired" 로 거절   url_expired   (백엔드가 새 주소로 다시 호출)
                      최대 크기 초과                       too_large
                      그 밖에 받지 못함                     download_failed (백엔드가 다시 호출. 작업자는 다시 시도하지 않는다)

허용 저장소(RAG_UPLOAD_ALLOWED_HOSTS)가 없으면 업로드 문서를 받지 않는다. 서버가 요청이 정한 주소로 접속하므로
콜백 허용 목록(비면 모두 허용, 개발용)보다 엄격하게 둔다. 리다이렉트는 따라가지 않는다.
fileUrl 에는 접근 서명이 들어 있으므로 로그·오류 글에 남기지 않는다. 받은 파일은 디스크에 쓰지 않는다.
"""
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from .callback import allowed_hosts_from_env
from .errors import DOWNLOAD_FAILED, TOO_LARGE, URL_EXPIRED, IngestError

DEFAULT_MAX_MB = 20               # 정해 둔 값. SCRUM-110 에서 재서 조정한다
DEFAULT_TIMEOUT_S = 30.0
_READ_BLOCK = 64 * 1024
_EXPIRED_MARK = b"Request has expired"   # 서명 주소가 만료되면 S3 가 403 본문에 담는 글


class FileUrlNotAllowed(ValueError):
    """fileUrl 이 형식에 맞지 않거나 허용한 저장소가 아니다."""


@dataclass(frozen=True)
class UploadSettings:
    allowed_hosts: frozenset | None       # None 이면 업로드 문서를 받지 않는다
    max_bytes: int
    timeout_s: float

    @property
    def enabled(self) -> bool:
        return bool(self.allowed_hosts)


UPLOAD_OFF = UploadSettings(allowed_hosts=None, max_bytes=0, timeout_s=0)


def settings_from_env(env) -> UploadSettings:
    max_mb = float(env.get("RAG_UPLOAD_MAX_MB") or DEFAULT_MAX_MB)
    return UploadSettings(allowed_hosts=allowed_hosts_from_env(env, key="RAG_UPLOAD_ALLOWED_HOSTS"),
                          max_bytes=int(max_mb * 1024 * 1024),
                          timeout_s=float(env.get("RAG_UPLOAD_TIMEOUT_S") or DEFAULT_TIMEOUT_S))


def check_file_url(url, settings: UploadSettings) -> None:
    """허용하지 않는 주소면 FileUrlNotAllowed. 오류 글에 주소를 넣지 않는다."""
    if not settings.enabled:
        raise FileUrlNotAllowed("업로드 문서를 받도록 설정되지 않음 (RAG_UPLOAD_ALLOWED_HOSTS)")
    if not isinstance(url, str) or not url:
        raise FileUrlNotAllowed("fileUrl 이 없음")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FileUrlNotAllowed("fileUrl 은 http 또는 https 주소여야 함")
    if parts.username or parts.password:
        raise FileUrlNotAllowed("fileUrl 에 계정 정보를 넣을 수 없음")
    host = parts.hostname.lower()
    hostport = f"{host}:{parts.port}" if parts.port else host
    if host not in settings.allowed_hosts and hostport not in settings.allowed_hosts:
        raise FileUrlNotAllowed("fileUrl 의 호스트가 허용한 저장소가 아님")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url, settings: UploadSettings) -> bytes:
    """파일을 받아 bytes 로 돌려준다. 처리할 때도 주소를 다시 확인한다 (설정이 바뀌었을 수 있으므로)."""
    try:
        check_file_url(url, settings)
    except FileUrlNotAllowed as e:
        raise IngestError(DOWNLOAD_FAILED, str(e)) from None
    try:
        with _OPENER.open(urllib.request.Request(url, method="GET"), timeout=settings.timeout_s) as r:
            length = r.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > settings.max_bytes:
                raise IngestError(TOO_LARGE, f"파일 {int(length)}바이트, 상한 {settings.max_bytes}바이트")
            return _read_capped(r, settings.max_bytes)
    except urllib.error.HTTPError as e:
        body = _safe_read(e)
        if e.code == 403 and _EXPIRED_MARK in body:
            raise IngestError(URL_EXPIRED, "서명 주소 만료") from None
        raise IngestError(DOWNLOAD_FAILED, f"HTTP {e.code}") from None
    except IngestError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as e:       # 연결 실패, 시간 초과 등
        raise IngestError(DOWNLOAD_FAILED, type(e).__name__) from None


def _read_capped(r, max_bytes) -> bytes:
    """상한을 넘으면 거기서 멈춘다 (Content-Length 가 없거나 틀려도)."""
    parts, size = [], 0
    while True:
        block = r.read(_READ_BLOCK)
        if not block:
            return b"".join(parts)
        size += len(block)
        if size > max_bytes:
            raise IngestError(TOO_LARGE, f"파일이 상한 {max_bytes}바이트를 넘음")
        parts.append(block)


def _safe_read(e, limit=4096) -> bytes:
    try:
        return e.read(limit) or b""
    except Exception:
        return b""
