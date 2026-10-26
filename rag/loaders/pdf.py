"""PDF -> Piece 목록. 쪽 하나가 Piece 하나다. SCRUM-107.

PDFium(pypdfium2, 크롬의 PDF 엔진)으로 글을 꺼낸다. pypdf 는 글자 폭 정보가 어긋난 PDF 에서 글자마다 띄어 꺼내는
일이 있어(합성 샘플로 확인, make_upload_samples.py) 쓰지 않는다.

  location = {"type": "page", "page": 쪽 번호, "label": "3쪽"}   (RAG 명세)
  글이 없는 쪽은 건너뛰고, 모든 쪽에 글이 없으면(스캔한 PDF 등) no_text
  암호 PDF → encrypted, 열 수 없는 PDF → unsupported_format, 쪽 수 상한 초과 → too_large

정리는 줄바꿈 맞추기와 겹친 공백 줄이기만 한다. 머리글·바닥글·쪽 번호 빼기는 SCRUM-110 에서 정한다.
꺼낸 글은 로그·오류 글에 남기지 않는다.
"""
import re

from ..errors import ENCRYPTED, NO_TEXT, TOO_LARGE, UNSUPPORTED_FORMAT, IngestError
from ..models import Piece
from .minutes import LoadResult

MAX_PAGES = 1000                 # 정해 둔 값. SCRUM-110 에서 재서 조정한다
_SPACES = re.compile(r"[ \t 　]+")


def load_pdf(data: bytes) -> LoadResult:
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        if "password" in str(e).lower():
            raise IngestError(ENCRYPTED, "암호 PDF") from None
        raise IngestError(UNSUPPORTED_FORMAT, "열 수 없는 PDF") from None
    try:
        if len(doc) > MAX_PAGES:
            raise IngestError(TOO_LARGE, f"{len(doc)}쪽, 상한 {MAX_PAGES}쪽")
        pieces, skipped = [], 0
        for i in range(len(doc)):
            page = doc[i]
            try:
                text = clean(page.get_textpage().get_text_range())
            finally:
                page.close()
            if not text:
                skipped += 1
                continue
            n = i + 1
            pieces.append(Piece(content=text, location={"type": "page", "page": n, "label": f"{n}쪽"}, kind="page"))
    finally:
        doc.close()
    if not pieces:
        raise IngestError(NO_TEXT, f"글을 꺼낸 쪽 없음 ({skipped}쪽, 스캔한 PDF 등)")
    return LoadResult(pieces, skipped)


def clean(text: str) -> str:
    """줄바꿈을 \\n 으로 맞추고, 줄마다 겹친 공백을 한 칸으로 줄이고, 빈 줄을 뺀다."""
    lines = (_SPACES.sub(" ", line).strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return "\n".join(line for line in lines if line)
