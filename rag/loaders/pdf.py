"""PDF -> Piece 목록. 쪽 안에서 번호 제목마다 Piece 하나다. SCRUM-107, 나누기는 SCRUM-110.

PDFium(pypdfium2, 크롬의 PDF 엔진)으로 글을 꺼낸다. pypdf 는 글자 폭 정보가 어긋난 PDF 에서 글자마다 띄어 꺼내는
일이 있어(합성 샘플로 확인, make_upload_samples.py) 쓰지 않는다.

  1. 쪽마다 글을 꺼낸다. 글이 없는 쪽은 건너뛴다
  2. 머리글·바닥글을 지운다: 쪽의 첫 두 줄·끝 두 줄 안에서, 글이 있는 쪽의 절반 이상(2쪽 이상)에 되풀이되는 줄.
     숫자는 같은 것으로 본다 ("- 1 -" 과 "- 2 -")
  3. 쪽 안에서 번호 제목 줄("1. 행사 개요", "3.2 2일차")이 나올 때마다 새 Piece 를 시작한다.
     제목 한 줄뿐인 Piece 는 넣지 않는다 (아래 Piece 의 context 에 들어간다).
     쪽 첫 부분은 앞 쪽의 마지막 제목을 이어받는다 (쪽을 넘어가는 표·문단)
  location = {"type": "page", "page": 쪽 번호, "label": "3쪽"}   (RAG 명세. 한 쪽의 Piece 는 모두 같은 쪽)
  context  = 제목 경로 ("3. 당일 진행 순서 > 3.2 2일차"). 검색할 때 조각 앞에 붙는다
쪽을 통째로 넣던 때와 비교한 결과는 SCRUM-110 (운영계획서 PDF 질문 35개, Hit@1 49% → 74%).
번호 없는 제목("개요", "가. 목적")은 나누지 못한다. 그런 쪽은 나누기(chunking)가 길이로만 자른다.

모든 쪽에 글이 없으면(스캔한 PDF 등) no_text. 암호 PDF → encrypted, 열 수 없는 PDF → unsupported_format,
쪽 수 상한 초과 → too_large. 꺼낸 글은 로그·오류 글에 남기지 않는다.
"""
import re
from collections import Counter

from ..errors import ENCRYPTED, NO_TEXT, TOO_LARGE, UNSUPPORTED_FORMAT, IngestError
from ..models import Piece
from .minutes import LoadResult
from .text import normalize

MAX_PAGES = 1000                 # 정해 둔 값. SCRUM-110 에서 재서 조정한다
EDGE_LINES = 2                   # 머리글·바닥글을 찾는 쪽 위·아래 줄 수
HEADING_MAX = 40                 # 제목으로 보는 줄의 최대 글자 수

# "1. 개요", "1.2 일시", "10. 비상 대응". "2026 고객"(점 없음), "1.5배 늘었다."(마침표로 끝남)는 제목이 아니다
_HEADING = re.compile(r"^(\d{1,2}\.(?:\d{1,2}\.?)*)\s+\S")


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
        pages, skipped = [], 0                  # [(쪽 번호, [줄])]
        for i in range(len(doc)):
            page = doc[i]
            try:
                text = normalize(page.get_textpage().get_text_range())
            finally:
                page.close()
            if text:
                pages.append((i + 1, text.split("\n")))
            else:
                skipped += 1
    finally:
        doc.close()
    pages = strip_header_footer(pages)
    skipped += sum(not lines for _, lines in pages)
    pieces, dropped = split_by_heading([(n, lines) for n, lines in pages if lines])
    if not pieces:
        raise IngestError(NO_TEXT, f"글을 꺼낸 쪽 없음 ({skipped}쪽, 스캔한 PDF 등)")
    return LoadResult(pieces, skipped + dropped)


def strip_header_footer(pages):
    """쪽의 위·아래 EDGE_LINES 줄 안에서 글이 있는 쪽의 절반 이상(2쪽 이상)에 되풀이되는 줄을 지운다."""
    def key(line):
        return re.sub(r"\d+", "#", line)

    def edge(lines):
        return set(range(min(EDGE_LINES, len(lines)))) | set(range(max(0, len(lines) - EDGE_LINES), len(lines)))

    seen = Counter()
    for _, lines in pages:
        seen.update({key(lines[i]) for i in edge(lines)})
    repeated = {k for k, count in seen.items() if count >= max(2, len(pages) / 2)}
    return [(n, [line for i, line in enumerate(lines) if not (i in edge(lines) and key(line) in repeated)])
            for n, lines in pages]


def heading_level(line) -> int | None:
    """번호 제목이면 단계("3." 1, "3.2" 2), 아니면 None."""
    m = _HEADING.match(line)
    if not m or len(line) > HEADING_MAX or line.endswith("."):
        return None
    return m.group(1).rstrip(".").count(".") + 1


def split_by_heading(pages):
    """[(쪽 번호, [줄])] → (Piece 목록, 넣지 않은 제목 한 줄 Piece 수)."""
    pieces, path, dropped = [], [], 0
    for n, lines in pages:
        buf = []

        def flush():
            nonlocal dropped
            if len(buf) == 1 and path and buf[0] == path[-1][1]:
                dropped += 1
            elif buf:
                pieces.append(Piece(content="\n".join(buf), kind="page",
                                    location={"type": "page", "page": n, "label": f"{n}쪽"},
                                    context=" > ".join(t for _, t in path) or None))
            buf.clear()

        for line in lines:
            level = heading_level(line)
            if level is not None:
                flush()
                path[:] = [(lv, t) for lv, t in path if lv < level] + [(level, line)]
            buf.append(line)
        flush()
    return pieces, dropped
