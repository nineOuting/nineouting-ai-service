"""워드(docx) -> Piece 목록. 제목 하나 아래의 글이 Piece 하나다. SCRUM-108.

본문을 위에서부터 순서대로 읽는다 (문단과 표가 섞인 순서 그대로).
  제목(제목 1~9 스타일, 문서 제목 스타일, 또는 개요 수준이 있는 문단)이 나오면 새 Piece 를 시작한다
  그 아래 문단·목록·표는 같은 Piece 에 이어 붙인다. 제목 글도 Piece 의 첫 줄로 넣는다
  바로 아래에 소제목이 와서 제목 한 줄뿐인 Piece 는 넣지 않는다 (소제목의 context 에 상위 제목이 들어간다)
  표는 행마다 한 줄. 첫 행을 머리 행으로 보고 "항목: 마케팅 | 금액: 12,000,000원" 처럼 쓴다. 병합된 칸은 한 번만
  location = {"type": "section", "heading": 가장 가까운 제목, "label": 같은 글}   (RAG 명세)
             첫 제목 전에 나오는 글은 heading "" 에 label "문서 처음"
  context  = 제목 경로 ("2. 예산 계획 > 2.1 마케팅"). 검색할 때 조각 앞에 붙는다

머리글·바닥글은 본문과 따로 저장되므로 읽지 않는다 (PDF 와 달리 정제가 필요 없다).
읽지 못하는 것: 글상자·도형 안의 글, 각주·미주, 워드가 붙이는 자동 번호("1.", "가." 등은 글자로 저장되지 않음).
글이 없으면 no_text, 열 수 없으면 unsupported_format. 꺼낸 글은 로그·오류 글에 남기지 않는다.
"""
import io
import re

from ..errors import NO_TEXT, UNSUPPORTED_FORMAT, IngestError
from ..models import Piece
from .minutes import LoadResult
from .text import header_line, normalize, row_line

START_LABEL = "문서 처음"
_HEADING_STYLE = re.compile(r"^heading (\d)$", re.I)


def load_docx(data: bytes) -> LoadResult:
    from docx import Document
    from docx.table import Table

    try:
        doc = Document(io.BytesIO(data))
        blocks = list(doc.iter_inner_content())
    except Exception:                       # 깨진 zip, 빠진 부분, XML 오류 등. 내용은 오류에 남기지 않는다
        raise IngestError(UNSUPPORTED_FORMAT, "열 수 없는 워드 파일") from None

    pieces, path, lines, skipped = [], [], [], 0

    def flush():
        nonlocal skipped
        heading_only = len(lines) == 1 and path and lines[0] == path[-1][1]
        text = "" if heading_only else normalize("\n".join(lines))
        skipped += bool(heading_only)
        if text:
            heading = path[-1][1] if path else ""
            pieces.append(Piece(content=text, kind="section",
                                location={"type": "section", "heading": heading, "label": heading or START_LABEL},
                                context=" > ".join(t for _, t in path) or None))
        lines.clear()

    for block in blocks:
        if isinstance(block, Table):
            rows = table_lines(block)
            lines.extend(rows)
            skipped += not rows
            continue
        text = normalize(block.text)
        level = heading_level(block)
        if level is not None and text:
            flush()
            path[:] = [(lv, t) for lv, t in path if lv < level] + [(level, text)]
            lines.append(text)
        elif text:
            lines.append(text)
        else:
            skipped += 1
    flush()
    if not pieces:
        raise IngestError(NO_TEXT, "본문에 글이 없음")
    return LoadResult(pieces, skipped)


def heading_level(paragraph) -> int | None:
    """제목이면 수준(문서 제목 0, 제목 1~9), 아니면 None. 스타일 이름과 개요 수준을 스타일 상속을 따라 본다."""
    style = paragraph.style
    level = _outline_level(paragraph._p.pPr)
    while level is None and style is not None:
        name = (style.name or "").strip()
        if name.lower() == "title":
            return 0
        m = _HEADING_STYLE.match(name)
        if m:
            return int(m.group(1))
        level = _outline_level(style.element.pPr)
        style = style.base_style
    return level


def _outline_level(ppr) -> int | None:
    """w:outlineLvl 값(0부터)을 제목 수준(1부터)으로. 9 는 "본문" 이라 제목이 아니다."""
    if ppr is None:
        return None
    from docx.oxml.ns import qn
    el = ppr.find(qn("w:outlineLvl"))
    if el is None:
        return None
    value = int(el.get(qn("w:val")))
    return value + 1 if value < 9 else None


def table_lines(table) -> list[str]:
    """표를 행마다 한 줄로. 첫 행을 머리 행으로 본다. 빈 행은 건너뛴다."""
    rows = [_row_texts(row) for row in table.rows]
    if not rows:
        return []
    header, out = rows[0], []
    if len(rows) == 1:
        line = header_line(header)
        return [line] if line else []
    for row in rows[1:]:
        line = row_line(header, row)
        if line:
            out.append(line)
    if not out and any(header):
        out.append(header_line(header))
    return out


def _row_texts(row) -> list[str]:
    """행의 칸 글. 가로로 병합된 칸은 같은 칸이 여러 번 나오므로 한 번만 쓴다."""
    seen, out = set(), []
    for cell in row.cells:
        key = id(cell._tc)
        if key in seen:
            continue
        seen.add(key)
        out.append(normalize(cell.text).replace("\n", " "))
    return out
