"""업로드 문서의 형식 알아내기. 파일 앞부분과 안쪽 모양으로 판단하고, 파일 이름의 확장자는 믿지 않는다. SCRUM-106.

  pdf    %PDF- 로 시작 (앞 1024바이트 안). 안에 /Encrypt 가 있으면 암호 PDF → encrypted
  docx   zip 이고 안에 word/document.xml
  xlsx   zip 이고 안에 xl/workbook.xml
  text   위 셋이 아니고 글자로 읽히는 파일 (txt·md, csv 등). 아래 decode_text. SCRUM-309
  그 밖  사용자가 묶은 zip, 옛 doc·xls, hwp, 이미지, 깨진 파일 → unsupported_format

워드·엑셀은 안쪽이 zip 이다. 그래서 푼 크기가 지나치게 큰 파일(압축 폭탄)을 여기서 막는다 → too_large.
오피스 문서에 암호를 걸면 zip 이 아니라 OLE(옛 오피스 저장 방식) 파일 안에 EncryptionInfo 와 EncryptedPackage 를
넣어 저장한다 → encrypted. 옛 doc·xls 와 hwp 도 OLE 파일이지만 이 표시가 없으므로 unsupported_format 이다.
hwpx 는 zip 이지만 워드·엑셀이 아니므로 unsupported_format 이다.
"""
import codecs
import io
import re
import zipfile

from .errors import ENCRYPTED, TOO_LARGE, UNSUPPORTED_FORMAT, IngestError

PDF, DOCX, XLSX, TEXT = "pdf", "docx", "xlsx", "text"
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024     # 정해 둔 값. SCRUM-110 에서 재서 조정한다
MAX_ZIP_RATIO = 100                         # 푼 크기 / 파일 크기. 보통 문서는 이보다 훨씬 작다 (추정)
MAX_CONTROL_RATIO = 0.01                    # 글자 중 제어 문자 비율 상한. 정해 둔 값 (측정 아님)
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE_ENCRYPTION = "EncryptionInfo".encode("utf-16-le")
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")   # zip 의 첫 파일 머리, 빈 zip. "PK" 만 보면 "PKI 정리" 같은 글이 zip 으로 잡힌다
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
_CONTROL = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f]")   # 줄바꿈(\n \r)·탭·쪽 넘김(\f)은 글에도 나오므로 뺀다


def detect(data: bytes) -> str:
    """PDF / DOCX / XLSX / TEXT 중 하나. 아니면 IngestError (unsupported_format, encrypted, too_large)."""
    if b"%PDF-" in data[:1024]:
        if b"/Encrypt" in data:
            raise IngestError(ENCRYPTED, "암호 PDF")
        return PDF
    if data.startswith(_OLE_MAGIC):
        if _OLE_ENCRYPTION in data:
            raise IngestError(ENCRYPTED, "암호 오피스 문서")
        raise IngestError(UNSUPPORTED_FORMAT, "OLE 파일 (옛 doc·xls, hwp 등)")
    if data.startswith(_ZIP_MAGIC):
        return _office_kind(data)
    if decode_text(data) is not None:
        return TEXT
    raise IngestError(UNSUPPORTED_FORMAT, "pdf·docx·xlsx·텍스트가 아님")


def decode_text(data: bytes) -> str | None:
    """글자로 읽히면 글, 아니면 None. 빈 파일은 "" (글이 없음은 로더가 no_text 로 알린다).

      UTF-16   앞에 BOM 이 있을 때만 (윈도우 메모장의 "유니코드" 저장)
      그 밖    0바이트가 있으면 글이 아님 (이미지·실행 파일 등). UTF-8(BOM 은 뗌) → CP949(옛 한글 윈도우) 순서로 읽는다
    CP949 는 아무 바이트나 꽤 읽혀 버리므로, 읽은 글에 제어 문자가 MAX_CONTROL_RATIO 보다 많으면 글이 아닌 것으로 본다.
    """
    if data.startswith(_UTF16_BOMS):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    elif b"\x00" in data:
        return None
    else:
        for encoding in ("utf-8-sig", "cp949"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return None
    if text and len(_CONTROL.findall(text)) > MAX_CONTROL_RATIO * len(text):
        return None
    return text


def _office_kind(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, ValueError):
        raise IngestError(UNSUPPORTED_FORMAT, "깨진 zip") from None
    unzipped = sum(i.file_size for i in infos)
    if unzipped > MAX_UNZIPPED_BYTES or unzipped > MAX_ZIP_RATIO * max(len(data), 1):
        raise IngestError(TOO_LARGE, f"푼 크기 {unzipped}바이트 (파일 {len(data)}바이트)")
    names = {i.filename for i in infos}
    if "[Content_Types].xml" in names and "word/document.xml" in names:
        return DOCX
    if "[Content_Types].xml" in names and "xl/workbook.xml" in names:
        return XLSX
    raise IngestError(UNSUPPORTED_FORMAT, "zip 이지만 워드·엑셀 문서가 아님")
