"""업로드 문서 받기와 형식 알아내기 시험 (SCRUM-106). 가짜 저장소 서버와 합성 파일을 쓴다. DB 는 필요 없다."""
import pytest

from rag.download import UPLOAD_OFF, FileUrlNotAllowed, check_file_url, fetch, settings_from_env
from rag.errors import DOWNLOAD_FAILED, ENCRYPTED, TOO_LARGE, UNSUPPORTED_FORMAT, URL_EXPIRED, IngestError
from rag.filetype import DOCX, PDF, XLSX, detect
from upload_fakes import office_bytes, ole_bytes, pdf_bytes, plain_zip, zip_bomb


def code_of(fn, *args):
    with pytest.raises(IngestError) as e:
        fn(*args)
    return e.value.code


# ---- 설정과 주소 확인 ----

def test_설정이_없으면_업로드를_받지_않는다():
    s = settings_from_env({})
    assert not s.enabled and s.max_bytes == 20 * 1024 * 1024 and s.timeout_s == 30.0
    with pytest.raises(FileUrlNotAllowed, match="설정되지 않음"):
        check_file_url("https://bucket.s3.test/a.pdf", s)


def test_설정값을_읽는다():
    s = settings_from_env({"RAG_UPLOAD_ALLOWED_HOSTS": "Bucket.S3.test, other:9000", "RAG_UPLOAD_MAX_MB": "5",
                           "RAG_UPLOAD_TIMEOUT_S": "7"})
    assert s.allowed_hosts == {"bucket.s3.test", "other:9000"} and s.max_bytes == 5 * 1024 * 1024 and s.timeout_s == 7


@pytest.mark.parametrize("url, word", [
    (None, "fileUrl 이 없음"),
    ("", "fileUrl 이 없음"),
    ("ftp://bucket.s3.test/a.pdf", "http 또는 https"),
    ("https://user:pw@bucket.s3.test/a.pdf", "계정 정보"),
    ("https://evil.test/a.pdf", "허용한 저장소가 아님"),
    ("http://169.254.169.254/latest/meta-data", "허용한 저장소가 아님"),
])
def test_허용하지_않는_주소는_거절하고_오류에_주소를_남기지_않는다(url, word):
    s = settings_from_env({"RAG_UPLOAD_ALLOWED_HOSTS": "bucket.s3.test"})
    with pytest.raises(FileUrlNotAllowed, match=word) as e:
        check_file_url(url, s)
    assert "evil" not in str(e.value) and "169.254" not in str(e.value)


def test_허용한_저장소면_통과():
    check_file_url("https://bucket.s3.test/ws/1/a.pdf?X-Amz-Signature=abc",
                   settings_from_env({"RAG_UPLOAD_ALLOWED_HOSTS": "bucket.s3.test"}))


# ---- 받기 ----

def test_파일을_메모리로_받는다(storage):
    storage.files["/a.pdf"] = pdf_bytes()
    assert fetch(storage.url("/a.pdf"), storage.settings()) == pdf_bytes()


@pytest.mark.parametrize("path, code", [
    ("/expired", URL_EXPIRED),            # S3 가 서명 만료로 거절
    ("/forbidden", DOWNLOAD_FAILED),      # 만료가 아닌 403
    ("/none.pdf", DOWNLOAD_FAILED),       # 404
    ("/error", DOWNLOAD_FAILED),          # 500
    ("/slow", DOWNLOAD_FAILED),           # 시간 초과
    ("/redirect", DOWNLOAD_FAILED),       # 리다이렉트는 따라가지 않는다
])
def test_받지_못하면_만료와_받기_실패를_나눈다(storage, path, code):
    assert code_of(fetch, storage.url(path), storage.settings(timeout_s=0.5)) == code


def test_연결할_수_없으면_받기_실패():
    s = settings_from_env({"RAG_UPLOAD_ALLOWED_HOSTS": "127.0.0.1:1", "RAG_UPLOAD_TIMEOUT_S": "1"})
    assert code_of(fetch, "http://127.0.0.1:1/a.pdf", s) == DOWNLOAD_FAILED


def test_처리할_때도_허용_저장소를_다시_확인한다(storage):
    storage.files["/a.pdf"] = pdf_bytes()
    assert code_of(fetch, storage.url("/a.pdf"), UPLOAD_OFF) == DOWNLOAD_FAILED
    assert storage.hits == []                                       # 접속하지 않음


def test_크기_상한을_넘으면_too_large(storage):
    storage.files["/big.pdf"] = b"%PDF-" + b"x" * 2000
    assert code_of(fetch, storage.url("/big.pdf"), storage.settings(max_bytes=1000)) == TOO_LARGE
    # Content-Length 가 없어도 읽으면서 멈춘다
    assert code_of(fetch, storage.url("/nolength/big.pdf"), storage.settings(max_bytes=1000)) == TOO_LARGE


def test_오류_글에_서명_주소가_남지_않는다(storage):
    with pytest.raises(IngestError) as e:
        fetch(storage.url("/error"), storage.settings())
    assert "Signature" not in str(e.value) and storage.host not in str(e.value)


# ---- 형식 알아내기 ----

def test_형식을_파일_안쪽으로_알아낸다():
    assert detect(pdf_bytes()) == PDF
    assert detect(b"\xef\xbb\xbf  " + pdf_bytes()) == PDF                # 앞에 몇 바이트가 붙어도
    assert detect(office_bytes("docx")) == DOCX
    assert detect(office_bytes("xlsx")) == XLSX


@pytest.mark.parametrize("data, code", [
    (pdf_bytes(encrypted=True), ENCRYPTED),
    (ole_bytes(encrypted=True), ENCRYPTED),          # 암호 건 워드·엑셀
    (ole_bytes(encrypted=False), UNSUPPORTED_FORMAT),  # 옛 doc·xls, hwp
    (plain_zip(), UNSUPPORTED_FORMAT),               # 사용자가 묶은 zip
    (b"PK\x03\x04 broken zip", UNSUPPORTED_FORMAT),
    (b"\x89PNG\r\n\x1a\n", UNSUPPORTED_FORMAT),
    ("회의 메모 텍스트".encode(), UNSUPPORTED_FORMAT),
    (b"", UNSUPPORTED_FORMAT),
    (zip_bomb("docx"), TOO_LARGE),                   # 압축 폭탄: 푼 크기가 파일 크기의 100배를 넘음
])
def test_받지_않는_파일은_이유를_나눈다(data, code):
    assert code_of(detect, data) == code
