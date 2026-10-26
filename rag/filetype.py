"""업로드 문서의 형식 알아내기. 파일 앞부분과 안쪽 모양으로 판단하고, 파일 이름의 확장자는 믿지 않는다. SCRUM-106.

  pdf    %PDF- 로 시작 (앞 1024바이트 안). 안에 /Encrypt 가 있으면 암호 PDF → encrypted
  docx   zip 이고 안에 word/document.xml
  xlsx   zip 이고 안에 xl/workbook.xml
  그 밖  사용자가 묶은 zip, 옛 doc·xls, hwp, 이미지, 깨진 파일 → unsupported_format

워드·엑셀은 안쪽이 zip 이다. 그래서 푼 크기가 지나치게 큰 파일(압축 폭탄)을 여기서 막는다 → too_large.
오피스 문서에 암호를 걸면 zip 이 아니라 OLE(옛 오피스 저장 방식) 파일 안에 EncryptionInfo 와 EncryptedPackage 를
넣어 저장한다 → encrypted. 옛 doc·xls 와 hwp 도 OLE 파일이지만 이 표시가 없으므로 unsupported_format 이다.
"""
import io
import zipfile

from .errors import ENCRYPTED, TOO_LARGE, UNSUPPORTED_FORMAT, IngestError

PDF, DOCX, XLSX = "pdf", "docx", "xlsx"
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024     # 정해 둔 값. SCRUM-110 에서 재서 조정한다
MAX_ZIP_RATIO = 100                         # 푼 크기 / 파일 크기. 보통 문서는 이보다 훨씬 작다 (추정)
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE_ENCRYPTION = "EncryptionInfo".encode("utf-16-le")


def detect(data: bytes) -> str:
    """PDF / DOCX / XLSX 중 하나. 아니면 IngestError (unsupported_format, encrypted, too_large)."""
    if b"%PDF-" in data[:1024]:
        if b"/Encrypt" in data:
            raise IngestError(ENCRYPTED, "암호 PDF")
        return PDF
    if data.startswith(_OLE_MAGIC):
        if _OLE_ENCRYPTION in data:
            raise IngestError(ENCRYPTED, "암호 오피스 문서")
        raise IngestError(UNSUPPORTED_FORMAT, "OLE 파일 (옛 doc·xls, hwp 등)")
    if data.startswith(b"PK"):
        return _office_kind(data)
    raise IngestError(UNSUPPORTED_FORMAT, "pdf·docx·xlsx 가 아님")


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
