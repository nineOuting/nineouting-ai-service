"""RAG 처리 오류. code 는 API 명세의 errorCode 값이다.

detail 은 로그에 남을 수 있으므로 문서 글을 넣지 않는다. 위치·개수 같은 구조 정보만 쓴다.
"""

NO_TEXT = "no_text"
UNSUPPORTED_FORMAT = "unsupported_format"   # 업로드 문서만
ENCRYPTED = "encrypted"                     # 업로드 문서만
DOWNLOAD_FAILED = "download_failed"         # 업로드 문서만
URL_EXPIRED = "url_expired"                 # 업로드 문서만
TOO_LARGE = "too_large"                     # 업로드 파일·푼 크기·쪽·칸, 또는 문서의 조각 토큰 합계가 상한을 넘음 [ai-service 제안]
INTERNAL_ERROR = "internal_error"


class IngestError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
