"""텍스트 파일(txt·md, csv 등 글자로 된 파일) -> Piece 목록. SCRUM-309.

글자 인코딩은 filetype.decode_text 가 알아낸다 (UTF-8, CP949, BOM 이 있는 UTF-16). 파일 이름이 오지 않으므로 txt 와 md 를
나누지 않고, 마크다운 제목이 하나라도 있으면 제목으로, 없으면 문단으로 나눈다.

  제목이 있는 글   줄 앞 "#"~"######" + 공백이 제목. 워드처럼 제목 하나 아래의 글이 Piece 하나 (docx.py 와 같은 규칙)
                   location = {"type": "section", "heading": 가장 가까운 제목, "label": 같은 글}, 첫 제목 전 글은 "문서 처음"
                   context  = 제목 경로 ("2. 예산 > 2.1 마케팅")
  제목이 없는 글   빈 줄로 나뉜 문단을 GROUP_CHARS 까지 이어 Piece 하나. 그보다 긴 문단은 줄 단위로 GROUP_CHARS 까지 끊는다
                   location = {"type": "line", "lines": [시작 줄, 끝 줄], "label": "12~18줄"}  (원본 파일의 줄 번호)

두 경우 모두 줄마다 다음을 정리한다. 나머지 마크다운 표시(**굵게**, "- " 목록 등)는 답변 근거가 원문과 같도록 그대로 둔다.
  코드 블록 울타리(``` · ~~~) 줄은 빼고 안의 글은 남긴다. 코드 블록 안의 "#" 은 제목으로 보지 않는다
  표(머리 행 다음 줄이 |---|---| 처럼 | 가 든 구분 줄)는 행마다 "열 이름: 값 | …" 한 줄 (워드·엑셀과 같은 row_line). 구분 줄은 뺀다
  링크 [글](주소)·그림 ![글](주소)는 글만 남긴다
읽지 못하는 것: 밑줄식 제목(다음 줄 === · ---), 참조식 링크([글][1]), HTML 태그. csv 는 표가 아니라 줄 그대로 들어간다.
글이 없으면 no_text, 글자로 읽히지 않으면 unsupported_format, 글이 MAX_CHARS 를 넘으면 too_large.
꺼낸 글은 로그·오류 글에 남기지 않는다.
"""
import re

from ..errors import NO_TEXT, TOO_LARGE, UNSUPPORTED_FORMAT, IngestError
from ..filetype import decode_text
from ..models import Piece
from .minutes import LoadResult
from .text import header_line, normalize, row_line

START_LABEL = "문서 처음"
# 한국어 글 1자 ≈ 0.57 토큰 (측정: 운영계획서 마크다운 0.56, 메모 0.58. e5-small-ko 토크나이저, 2026-11-18)
MAX_CHARS = 500_000        # ≈ 28만 토큰. 문서 처리량 상한(RAG_MAX_DOC_TOKENS 10만 ≈ 17.5만 자)이 보통 먼저 걸린다.
                           # 50MB 같은 큰 글을 토큰으로 세기 전에 끊는 용도
GROUP_CHARS = 400          # 제목 없는 글에서 문단을 이어 붙이는 글자 수. ≈ 230 토큰이라 청크 상한(256) 안에 든다.
                           # 넘으면 청크가 다시 나뉘어 같은 줄 범위를 가진 청크가 여럿 생긴다

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")


def load_text(data: bytes) -> LoadResult:
    text = decode_text(data)
    if text is None:
        raise IngestError(UNSUPPORTED_FORMAT, "글자로 읽히지 않는 파일")
    if len(text) > MAX_CHARS:
        raise IngestError(TOO_LARGE, f"글자 {len(text)}개, 상한 {MAX_CHARS}개")
    lines = clean_lines(text)
    if not any(t for _, t, _ in lines):
        raise IngestError(NO_TEXT, "글이 없음")
    if any(level for _, _, level in lines):
        pieces, skipped = by_heading(lines)
    else:
        pieces, skipped = by_paragraph(lines), 0
    if not pieces:
        raise IngestError(NO_TEXT, "글이 없음")
    return LoadResult(pieces, skipped)


def clean_lines(text: str) -> list[tuple[int, str, int]]:
    """원본 줄마다 (줄 번호, 정리한 글, 제목 수준). 빈 줄은 글 "" (문단 경계), 제목이 아니면 수준 0.
    줄 번호는 1부터, 편집기에 보이는 번호와 같다 (\\r\\n · \\r · \\n 만 줄바꿈으로 센다)."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, fence, i = [], None, 0
    while i < len(raw):
        line, number = raw[i], i + 1
        m = _FENCE.match(line)
        if m and (fence is None or (m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence))):
            fence = m.group(1) if fence is None else None
            i += 1
            continue
        if fence is None:
            heading = _HEADING.match(line)
            if heading and normalize(heading.group(2)):
                out.append((number, normalize(_LINK.sub(r"\1", heading.group(2))), len(heading.group(1))))
                i += 1
                continue
            if "|" in line and i + 1 < len(raw) and "|" in raw[i + 1] and _TABLE_RULE.match(raw[i + 1]):
                i = _table(raw, i, out)
                continue
        out.append((number, normalize(_LINK.sub(r"\1", line)), 0))
        i += 1
    return out


def _table(raw, i, out):
    """마크다운 표를 행마다 한 줄로 out 에 넣고, 표 다음 줄 번호(0부터)를 돌려준다."""
    header = _cells(raw[i])
    j, rows = i + 2, 0
    while j < len(raw) and "|" in raw[j] and raw[j].strip():
        line = row_line(header, _cells(raw[j]))
        if line:
            out.append((j + 1, line, 0))
            rows += 1
        j += 1
    if not rows:
        out.append((i + 1, header_line(header), 0))
    return j


def _cells(line):
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") and not s.endswith("\\|") else s
    return [normalize(_LINK.sub(r"\1", c)).replace("\\|", "|") for c in _CELL_SPLIT.split(s)]


def by_heading(lines):
    """제목 하나 아래의 글이 Piece 하나. 바로 아래에 소제목이 와서 제목 한 줄뿐인 Piece 는 넣지 않는다."""
    pieces, path, body, skipped = [], [], [], 0

    def flush():
        nonlocal skipped
        heading_only = len(body) == 1 and path and body[0] == path[-1][1]
        text = "" if heading_only else "\n".join(body)
        skipped += bool(heading_only)
        if text:
            heading = path[-1][1] if path else ""
            pieces.append(Piece(content=text, kind="section",
                                location={"type": "section", "heading": heading, "label": heading or START_LABEL},
                                context=" > ".join(t for _, t in path) or None))
        body.clear()

    for _, text, level in lines:
        if level:
            flush()
            path[:] = [(lv, t) for lv, t in path if lv < level] + [(level, text)]
            body.append(text)
        elif text:
            body.append(text)
    flush()
    return pieces, skipped


def by_paragraph(lines):
    """빈 줄로 나뉜 문단을 GROUP_CHARS 까지 이어 Piece 하나. GROUP_CHARS 보다 긴 문단은 줄 단위로 GROUP_CHARS 까지 끊는다
    (줄 하나가 더 길면 그 줄만으로 Piece 하나, 청크 나누기가 다시 자른다)."""
    blocks, current = [], []
    for number, text, _ in lines + [(0, "", 0)]:
        if text:
            current.append((number, text))
        elif current:
            blocks.extend(_fill(current))
            current = []
    groups = []
    for block in blocks:
        if groups and _size(groups[-1] + block) <= GROUP_CHARS:
            groups[-1] = groups[-1] + block
        else:
            groups.append(block)
    return [_line_piece(g) for g in groups]


def _fill(paragraph):
    """문단을 앞에서부터 GROUP_CHARS 까지 줄 단위로 채워 나눈다."""
    out = [[]]
    for line in paragraph:
        if out[-1] and _size(out[-1] + [line]) > GROUP_CHARS:
            out.append([])
        out[-1].append(line)
    return out


def _size(lines):
    return sum(len(t) + 1 for _, t in lines) - 1


def _line_piece(group):
    first, last = group[0][0], group[-1][0]
    label = f"{first}줄" if first == last else f"{first}~{last}줄"
    return Piece(content="\n".join(t for _, t in group), kind="line",
                 location={"type": "line", "lines": [first, last], "label": label})
