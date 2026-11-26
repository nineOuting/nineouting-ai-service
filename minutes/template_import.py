"""회의록 양식 가져오기 (REQ-195, SCRUM-289). 사용자가 올린 양식 PDF 에서 항목을 읽어 `template.fields` 모양으로 만든다.

  1. 파일 읽기      PDF, 워드(docx), 엑셀(xlsx), 글 파일(txt·md·csv). 형식은 확장자가 아니라 내용으로 판단한다 (rag/filetype.py).
                    글을 꺼내 줄마다 번호(L1, L2, ...)를 단다. 머리글·바닥글은 지우지 않는다 (양식의 header·footer 로 쓰기 위해).
                    PDF 는 쪽마다 꺼내고, 워드·엑셀·글 파일은 한 쪽으로 본다. 워드의 머리글·바닥글은 맨 앞과 맨 뒤 줄로 넣는다.
                    표는 칸을 " | "로 이어 한 줄로 쓴다 (워드·엑셀은 칸 경계를 알 수 있다). 글이 없으면(스캔 PDF 등) no_text.
                    옛 doc·xls, hwp·hwpx, pptx, 이미지는 unsupported_format
  2. AI 에게 묻기   줄 번호가 붙은 글을 주고 항목 제목의 줄 번호(ref), 기록 방식, 표의 열 개수, 설명 줄, 머리글·바닥글 줄을 받는다
  3. 코드 검사      제목이 그 줄에 실제로 있는지(한 줄에 "회의명 일시"처럼 칸 이름이 여럿이면 줄에 나온 순서대로 같은 줄 번호),
                    줄 번호가 있는지, 순서·중복·개수(1~20), 기록 방식, 표의 열 개수(센 값 1~12),
                    머리글은 첫 쪽의 위쪽 줄·바닥글은 마지막 쪽의 아래쪽 줄인지. 걸리면 이유를 붙여 다시 시킨다 (placement.ask)
  4. 글은 코드가 정함  설명·머리글·바닥글은 AI 가 쓴 글이 아니라 AI 가 고른 줄의 원문을 코드가 이어 붙인 것이다
                    체크리스트인데 체크박스 글자(☐ □ [ ])가 그 항목 안에 없으면 종류는 그대로 두고 경고(CHECKLIST_NO_BOX)를 남긴다.
                    설명 줄에 빈칸(____)이 있으면 서식 문장이라 설명에서 빼고 경고(DESCRIPTION_DROPPED)를 남긴다.
                    표의 열이 10보다 많으면 10으로 자르고 센 값을 경고(COLUMNS_CAPPED, detected)로 남긴다 (명세 5.8 의 상한)

결과 모양은 회의록 초안 명세 5.8 TemplateField 와 같다: {"title", "type", "columns"?, "description"?, "aiFill": true}.
`aiFill` 은 모두 true 로 둔다 (가져온 항목을 AI 가 채울지는 아직 정하지 않았다. docs/api/template-import-contract.md).
양식 글은 로그와 오류 글에 남기지 않는다 (개수·줄 번호·코드만).
"""
import io
import re
from dataclasses import dataclass, field

from rag.errors import ENCRYPTED, NO_TEXT, TOO_LARGE, UNSUPPORTED_FORMAT, IngestError
from rag.filetype import DOCX, PDF, TEXT, XLSX, decode_text, detect
from rag.loaders.text import normalize
from .placement import Outcome, Prompt, ask, load_prompt

KIND = "template_import"
PRODUCT_FORMATS = frozenset({PDF, DOCX})   # 서비스에서 받는 형식 (2026-11-24 기획 결정: PDF·워드만). 읽는 코드는 엑셀·글 파일도 읽을 수 있어 설정(MINUTES_IMPORT_FORMATS)으로 넓힌다
ALL_FORMATS = frozenset({PDF, DOCX, XLSX, TEXT})
NO_FIELDS = "NO_FIELDS"               # AI 가 항목을 하나도 찾지 못함 (양식이 아니거나 읽을 수 없음)

MAX_PAGES = 10                        # 제안값. 양식 PDF 는 짧다 (실제 양식 샘플로 확인 전)
MAX_CHARS = 20_000                    # 제안값. 한 번에 AI 에 보내는 글 크기 상한
MAX_FIELDS = 20                       # 회의록 초안 명세 5.1 template.fields 1~20개
MAX_TITLE = 100                       # 명세 5.8 title 1~100자
MAX_DESCRIPTION = 2000                # 명세 5.8 description
MAX_COLUMNS = 10                      # 명세 5.8 columns 1~10 (양식 편집 화면 상한과 같다). 이보다 많은 표는 10으로 자르고 경고(COLUMNS_CAPPED)로 센 값을 알린다
MAX_COUNTED_COLUMNS = 12              # AI 가 셀 수 있는 열 개수 (상한보다 넉넉히). 넘으면 다시 시킨다
EDGE_LINES = 5                        # 머리글은 쪽의 위 5줄, 바닥글은 아래 5줄 안에서만 받는다
TYPES = ("free", "list", "checklist", "table")
_BLANK = re.compile(r"_{3,}")
_BOX = re.compile(r"[☐□▢☑☒✓✔]|\[\s*[xX✓]?\s*\]")
_SPACE = re.compile(r"\s+")

CHECKLIST_NO_BOX = "CHECKLIST_NO_BOX"      # 체크리스트인데 그 항목 안에서 체크박스 글자를 못 찾음 (종류는 AI 가 고른 대로 둔다)
DESCRIPTION_DROPPED = "DESCRIPTION_DROPPED"  # 빈칸(____)이 든 서식 문장은 쓰는 방법 안내가 아니라 양식 본문이라 설명으로 쓰지 않고 뺐다
COLUMNS_CAPPED = "COLUMNS_CAPPED"          # 표의 열이 상한(10)보다 많아 10으로 잘랐다. detected 에 센 값


@dataclass(frozen=True)
class Line:
    ref: int          # 1부터. 문서 전체에서 이어서 센다
    page: int
    pos: int          # 그 쪽 안에서 몇 번째 줄 (0부터)
    page_len: int     # 그 쪽의 줄 수
    text: str


@dataclass
class Imported:
    fields: list[dict]
    header: str | None
    footer: str | None
    warnings: list[dict] = field(default_factory=list)     # {"code", "title"}


@dataclass(frozen=True)
class ImportOutcome:
    result: Imported | None
    error_code: str | None
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def read_lines(data: bytes, formats=None) -> list[Line]:
    """파일 바이트 -> 줄 목록. 읽을 수 없으면 IngestError (unsupported_format, encrypted, too_large, no_text).
    PDF, 워드(docx), 엑셀(xlsx), 글 파일(txt·md·csv)을 읽을 수 있다. 형식은 파일 내용으로 판단한다.
    formats: 받는 형식 집합(PDF, DOCX, XLSX, TEXT). 정하면 그 밖의 형식은 unsupported_format. None 이면 읽을 수 있는 형식 모두."""
    kind = detect(data)
    if formats is not None and kind not in formats:
        raise IngestError(UNSUPPORTED_FORMAT, f"{kind} 형식은 받지 않음")
    if kind == PDF:
        return number_lines(_pdf_pages(data))
    if kind == DOCX:
        return number_lines([(1, _docx_lines(data))])
    if kind == XLSX:
        return number_lines([(1, _xlsx_lines(data))])
    if kind == TEXT:
        return number_lines([(1, _text_lines(data))])
    raise IngestError(UNSUPPORTED_FORMAT, kind)


def _pdf_pages(data: bytes) -> list[tuple[int, list[str]]]:
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
        pages = []
        for i in range(len(doc)):
            page = doc[i]
            try:
                text = normalize(page.get_textpage().get_text_range())
            finally:
                page.close()
            if text:
                pages.append((i + 1, text.split("\n")))
    finally:
        doc.close()
    return pages


def _cells_line(cells) -> str:
    """표의 한 행 -> 한 줄. 빈 칸은 빼고 " | "로 잇는다."""
    return " | ".join(c for c in (normalize(str(c)).replace("\n", " ") for c in cells if c is not None) if c)


def _docx_lines(data: bytes) -> list[str]:
    """워드 -> 줄 목록: 머리글, 본문(문단과 표를 문서 순서대로), 바닥글. 글상자·도형·각주 안의 글은 읽지 못한다."""
    from docx import Document
    from docx.table import Table

    try:
        doc = Document(io.BytesIO(data))
        blocks = list(doc.iter_inner_content())
        header = [p.text for sec in doc.sections for p in sec.header.paragraphs]
        footer = [p.text for sec in doc.sections for p in sec.footer.paragraphs]
    except Exception:                                    # 깨진 zip, 빠진 부분, XML 오류 등. 내용은 오류에 남기지 않는다
        raise IngestError(UNSUPPORTED_FORMAT, "열 수 없는 워드 파일") from None
    out = []

    def add(text):
        text = normalize(text).replace("\n", " ")
        if text and (not out or out[-1] != text):       # 구역마다 되풀이되는 머리글·바닥글은 한 번만
            out.append(text)
    for t in header:
        add(t)
    for block in blocks:
        if isinstance(block, Table):
            seen = []                                    # 가로·세로로 병합된 칸은 표 전체에서 한 번만 (결재란 "결 재" 같은 칸).
            for row in block.rows:                       # 요소를 들고 있어야 같은 칸인지 `is`로 안정적으로 비교된다 (id() 는 요소가 버려지면 재사용된다)
                cells = []
                for cell in row.cells:
                    if not any(cell._tc is t for t in seen):
                        seen.append(cell._tc)
                        cells.append(cell.text)
                add(_cells_line(cells))
        else:
            add(block.text)
    for t in footer:
        add(t)
    return out


def _xlsx_lines(data: bytes) -> list[str]:
    """엑셀 -> 줄 목록: 보이는 시트마다 글이 있는 행을 " | "로 이은 한 줄. 시트가 둘 이상이면 시트 이름 줄을 앞에 둔다."""
    from openpyxl import load_workbook
    from rag.loaders.xlsx import MAX_CELLS

    try:
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception:                                    # 깨진 zip, 빠진 부분, XML 오류 등
        raise IngestError(UNSUPPORTED_FORMAT, "열 수 없는 엑셀 파일") from None
    try:
        cells = sum(ws.max_row * ws.max_column for ws in wb.worksheets)
        if cells > MAX_CELLS:
            raise IngestError(TOO_LARGE, f"칸 {cells}개, 상한 {MAX_CELLS}개")
        sheets = [ws for ws in wb.worksheets if ws.sheet_state == "visible"]
        out = []
        for ws in sheets:
            if len(sheets) > 1:
                out.append(normalize(ws.title))
            for row in ws.iter_rows(values_only=True):
                line = _cells_line(row)
                if line:
                    out.append(line)
        return out
    finally:
        wb.close()


_MD_SEPARATOR = re.compile(r"^:?-{3,}:?$")


def _text_lines(data: bytes) -> list[str]:
    """글 파일(txt·md·csv) -> 줄 목록. 마크다운의 #(제목)과 csv 의 쉼표는 그대로 둔다.
    마크다운 표("| 가 | 나 |")는 워드·엑셀 표와 같은 모양("가 | 나")으로 정리한다: 양 끝의 | 와 구분 줄(|---|---|)을 빼고,
    빈 칸은 뺀다. 그대로 두면 모델이 열 개수를 하나 더 세는 일이 반복됐다 (6열을 7열로)."""
    text = decode_text(data) or ""
    out = []
    for line in text.split("\n"):
        line = normalize(line)
        if line.startswith("|") and line.endswith("|") and len(line) > 1:
            cells = [c.strip() for c in line[1:-1].split("|")]
            if all(_MD_SEPARATOR.match(c) for c in cells if c) and any(cells):
                continue                                 # |---|---| 구분 줄
            line = " | ".join(c for c in cells if c)
        if line:
            out.append(line)
    return out


def number_lines(pages: list[tuple[int, list[str]]]) -> list[Line]:
    lines, ref = [], 0
    for page_no, texts in pages:
        for pos, text in enumerate(texts):
            ref += 1
            lines.append(Line(ref, page_no, pos, len(texts), text))
    if not lines:
        raise IngestError(NO_TEXT, "글을 꺼낸 쪽 없음 (스캔한 PDF 등)")
    if sum(len(x.text) for x in lines) > MAX_CHARS:
        raise IngestError(TOO_LARGE, f"글 {sum(len(x.text) for x in lines)}자, 상한 {MAX_CHARS}자")
    return lines


def _quote(text: str) -> str:
    return text.replace("<", "‹").replace(">", "›")


def build_user(lines: list[Line], problems: list[str] = ()) -> str:
    parts = ["<양식 글>"]
    parts += [f"L{x.ref} ({x.page}쪽) {_quote(x.text)}" for x in lines]
    parts.append("</양식 글>")
    if problems:
        parts += ["<지난 답의 문제>", *(f"- {p}" for p in problems), "</지난 답의 문제>"]
    return "\n".join(parts)


def _pattern(text: str) -> str:
    """쪽 번호처럼 숫자만 다른 줄을 같은 글로 보기 위해 숫자를 지운다."""
    return re.sub(r"\d+", "#", _compact(text))


def _compact(text: str) -> str:
    return _SPACE.sub("", text).casefold()


def _ref_int(value):
    """줄 번호 하나 읽기. 정수 12 와 "12", "L12" 를 모두 받는다 (AI 가 입력에 보이는 "L12" 모양을 따라 쓰기 쉽다). 아니면 None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = re.fullmatch(r"\s*[Ll]?(\d+)\s*", value)
        return int(m.group(1)) if m else None
    return None


def _refs(raw, by_ref, name, problems) -> list[int]:
    """줄 번호 목록 읽기. 틀리면 문제를 적고 빈 목록."""
    if raw is None:
        return []
    nums = [_ref_int(r) for r in raw] if isinstance(raw, list) else None
    if nums is None or None in nums:
        problems.append(f"{name}가 줄 번호(정수)의 목록이 아님")
        return []
    raw = nums
    bad = [r for r in raw if r not in by_ref]
    if bad:
        problems.append(f"{name}에 없는 줄 번호 {', '.join(f'L{r}' for r in bad[:5])}")
        return []
    if len(set(raw)) != len(raw):
        problems.append(f"{name}에 같은 줄 번호가 두 번 있음")
    return sorted(set(raw))


def check(data, lines: list[Line]) -> tuple[Imported | None, list[str]]:
    """(가져온 양식, 문제 목록). 문제가 하나라도 있으면 양식은 None. 항목이 0개인 것은 문제가 아니다 (호출한 쪽이 NO_FIELDS 로 본다)."""
    if not isinstance(data, dict) or not isinstance(data.get("fields"), list):
        return None, ['답이 {"header": [...], "footer": [...], "fields": [...]} 모양의 JSON이 아님']
    by_ref = {x.ref: x for x in lines}
    problems: list[str] = []
    header = _refs(data.get("header"), by_ref, "header", problems)
    footer = _refs(data.get("footer"), by_ref, "footer", problems)
    first_page, last_page = lines[0].page, lines[-1].page
    in_header = lambda x: x.page == first_page and x.pos < EDGE_LINES                       # noqa: E731
    in_footer = lambda x: x.page == last_page and x.pos >= x.page_len - EDGE_LINES          # noqa: E731

    def zone(refs, ok, name):
        """머리글·바닥글은 매 쪽에 반복되므로 AI 가 다른 쪽의 같은 글(숫자만 다른 쪽 번호 포함)을 고르면 첫·마지막 쪽 것으로 본다."""
        keep = []
        for r in refs:
            x = by_ref[r]
            same = next((y for y in lines if ok(y) and _pattern(y.text) == _pattern(x.text)), None)
            if ok(x):
                keep.append(r)
            elif same is not None:
                keep.append(same.ref)
            else:
                problems.append(f"{name}의 L{r}이 {'첫 쪽의 위' if name == 'header' else '마지막 쪽의 아래'} {EDGE_LINES}줄 안에 있지 않음")
        return sorted(set(keep))

    header, footer = zone(header, in_header, "header"), zone(footer, in_footer, "footer")
    raw_fields = data["fields"]
    if len(raw_fields) > MAX_FIELDS:
        problems.append(f"항목이 {len(raw_fields)}개. 최대 {MAX_FIELDS}개")
    used = set(header) | set(footer)             # 항목이 될 수 없는 줄. 한 줄에 칸 이름이 여럿이면 항목끼리는 줄 번호를 같이 쓸 수 있다
    parsed, titles, cursor = [], {}, {}      # cursor: 줄마다 앞 항목의 제목이 끝난 위치 (한 줄에 칸 이름이 여럿일 때)
    for i, f in enumerate(raw_fields[:MAX_FIELDS + 1]):
        where = f"fields[{i}]"
        if not isinstance(f, dict):
            problems.append(f"{where}이 객체가 아님")
            continue
        ref, title, kind = _ref_int(f.get("ref")), f.get("title"), f.get("type")
        if ref is None or ref not in by_ref:
            problems.append(f"{where}의 ref가 없는 줄 번호")
            continue
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE:
            problems.append(f"{where}의 title이 1~{MAX_TITLE}자의 글이 아님")
            continue
        title = title.strip()
        found = _compact(by_ref[ref].text).find(_compact(title), cursor.get(ref, 0))
        if found < 0:
            problems.append(f"{where}의 title이 L{ref}의 글에 없거나, 같은 줄의 앞 항목보다 앞에 있음")
            continue
        cursor[ref] = found + len(_compact(title))
        if ref in used:
            problems.append(f"L{ref}는 머리글·바닥글로 쓴 줄이라 항목이 될 수 없음")
        if kind not in TYPES:
            problems.append(f"{where}의 type이 {'/'.join(TYPES)} 중 하나가 아님")
            continue
        columns = f.get("columns")
        if kind == "table":
            if isinstance(columns, bool) or not isinstance(columns, int) or not 1 <= columns <= MAX_COUNTED_COLUMNS:
                problems.append(f"{where}는 표인데 columns가 1~{MAX_COUNTED_COLUMNS}의 정수가 아님")
                continue
        else:
            columns = None
        key = _compact(title)
        if key in titles:
            problems.append(f"{where}의 title이 fields[{titles[key]}]와 같음")
        titles[key] = i
        desc_refs = _refs(f.get("descriptionRefs"), by_ref, f"{where}의 descriptionRefs", problems)
        parsed.append({"ref": ref, "title": title, "type": kind, "columns": columns, "desc": desc_refs})
    refs = [p["ref"] for p in parsed]
    if refs != sorted(refs):
        problems.append("항목은 문서에 나온 순서(ref 오름차순)여야 함")
    for p in parsed:
        clash = [r for r in p["desc"] if r in used or r in set(refs)]
        if clash:
            problems.append(f"설명 줄 {', '.join(f'L{r}' for r in clash[:3])}이 항목·머리글·바닥글과 겹침")
    if problems:
        return None, problems
    out, warnings = [], []
    ends = refs[1:] + [lines[-1].ref + 1]
    for p, end in zip(parsed, ends):
        kind = p["type"]
        if kind == "checklist" and not any(_BOX.search(x.text) for x in lines if p["ref"] <= x.ref < end):
            warnings.append({"code": CHECKLIST_NO_BOX, "title": p["title"]})
        item = {"title": p["title"], "type": kind, "aiFill": True}
        if kind == "table":
            item["columns"] = min(p["columns"], MAX_COLUMNS)
            if p["columns"] > MAX_COLUMNS:
                warnings.append({"code": COLUMNS_CAPPED, "title": p["title"], "detected": p["columns"]})
        description = " ".join(by_ref[r].text for r in p["desc"])
        if description and _BLANK.search(description):
            warnings.append({"code": DESCRIPTION_DROPPED, "title": p["title"]})
        elif description:
            if len(description) > MAX_DESCRIPTION:
                return None, [f"{p['title']}의 설명이 {MAX_DESCRIPTION}자를 넘음"]
            item["description"] = description
        out.append(item)
    join = lambda rs: "\n".join(by_ref[r].text for r in rs) or None     # noqa: E731
    return Imported(out, join(header), join(footer), warnings), []


def import_template(data: bytes, llm, timeout_s: float, max_retries: int = 2, call_retries: int = 1,
                    prompt: Prompt | None = None, formats=None) -> ImportOutcome:
    """양식 PDF -> ImportOutcome. 파일을 읽지 못하면 error_code 에 IngestError 의 코드(no_text 등), AI 쪽은 placement 의
    CHECK_FAILED / LLM_TIMEOUT / LLM_FAILED, 항목이 없으면 NO_FIELDS."""
    try:
        lines = read_lines(data, formats)
    except IngestError as e:
        return ImportOutcome(None, e.code)
    out: Outcome = ask(prompt or load_prompt(None, KIND), lambda problems: build_user(lines, problems),
                       lambda raw: check(raw, lines), llm, timeout_s, max_retries, call_retries)
    if out.placement is None:
        return ImportOutcome(None, out.error_code, out.attempts, out.input_tokens, out.output_tokens)
    if not out.placement.fields:
        return ImportOutcome(None, NO_FIELDS, out.attempts, out.input_tokens, out.output_tokens)
    return ImportOutcome(out.placement, None, out.attempts, out.input_tokens, out.output_tokens)
