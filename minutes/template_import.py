"""회의록 양식 가져오기 (REQ-195, SCRUM-289). 사용자가 올린 양식 PDF 에서 항목을 읽어 `template.fields` 모양으로 만든다.

  1. 파일 읽기      PDF 만 (docx 등은 아직 정하지 않음). 쪽마다 글을 꺼내 줄마다 번호(L1, L2, ...)를 단다.
                    머리글·바닥글은 지우지 않는다 (양식의 header·footer 로 쓰기 위해). 글이 없으면(스캔 PDF) no_text
  2. AI 에게 묻기   줄 번호가 붙은 글을 주고 항목 제목의 줄 번호(ref), 기록 방식, 표의 열 개수, 설명 줄, 머리글·바닥글 줄을 받는다
  3. 코드 검사      제목이 그 줄에 실제로 있는지, 줄 번호가 있는지, 순서·중복·개수(1~20), 기록 방식, 표의 열 개수(1~6),
                    머리글은 첫 쪽의 위쪽 줄·바닥글은 마지막 쪽의 아래쪽 줄인지. 걸리면 이유를 붙여 다시 시킨다 (placement.ask)
  4. 글은 코드가 정함  설명·머리글·바닥글은 AI 가 쓴 글이 아니라 AI 가 고른 줄의 원문을 코드가 이어 붙인 것이다
                    체크리스트인데 체크박스 글자(☐ □ [ ])가 그 항목 안에 없으면 목록(list)으로 낮추고 경고를 남긴다

결과 모양은 회의록 초안 명세 5.8 TemplateField 와 같다: {"title", "type", "columns"?, "description"?, "aiFill": true}.
`aiFill` 은 모두 true 로 둔다 (가져온 항목을 AI 가 채울지는 아직 정하지 않았다. docs/api/template-import-contract.md).
양식 글은 로그와 오류 글에 남기지 않는다 (개수·줄 번호·코드만).
"""
import re
from dataclasses import dataclass, field

from rag.errors import ENCRYPTED, NO_TEXT, TOO_LARGE, UNSUPPORTED_FORMAT, IngestError
from rag.filetype import PDF, detect
from rag.loaders.text import normalize
from .placement import Outcome, Prompt, ask, load_prompt

KIND = "template_import"
NO_FIELDS = "NO_FIELDS"               # AI 가 항목을 하나도 찾지 못함 (양식이 아니거나 읽을 수 없음)

MAX_PAGES = 10                        # 제안값. 양식 PDF 는 짧다 (실제 양식 샘플로 확인 전)
MAX_CHARS = 20_000                    # 제안값. 한 번에 AI 에 보내는 글 크기 상한
MAX_FIELDS = 20                       # 회의록 초안 명세 5.1 template.fields 1~20개
MAX_TITLE = 100                       # 명세 5.8 title 1~100자
MAX_DESCRIPTION = 2000                # 명세 5.8 description
MAX_COLUMNS = 6                       # 명세 5.8 columns 1~6
EDGE_LINES = 5                        # 머리글은 쪽의 위 5줄, 바닥글은 아래 5줄 안에서만 받는다
TYPES = ("free", "list", "checklist", "table")
_BOX = re.compile(r"[☐□▢☑☒✓✔]|\[\s*[xX✓]?\s*\]")
_SPACE = re.compile(r"\s+")

CHECKLIST_NO_BOX = "CHECKLIST_NO_BOX"


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


def read_lines(data: bytes) -> list[Line]:
    """PDF 바이트 -> 줄 목록. 읽을 수 없으면 IngestError (unsupported_format, encrypted, too_large, no_text)."""
    kind = detect(data)
    if kind != PDF:
        raise IngestError(UNSUPPORTED_FORMAT, f"{kind} (양식 가져오기는 PDF 만)")
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
    return number_lines(pages)


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


def _compact(text: str) -> str:
    return _SPACE.sub("", text).casefold()


def _refs(raw, by_ref, name, problems) -> list[int]:
    """줄 번호 목록 읽기. 틀리면 문제를 적고 빈 목록."""
    if raw is None:
        return []
    if not isinstance(raw, list) or any(isinstance(r, bool) or not isinstance(r, int) for r in raw):
        problems.append(f"{name}가 줄 번호(정수)의 목록이 아님")
        return []
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
    for r in header:
        x = by_ref[r]
        if x.page != first_page or x.pos >= EDGE_LINES:
            problems.append(f"header의 L{r}이 첫 쪽의 위 {EDGE_LINES}줄 안에 있지 않음")
    for r in footer:
        x = by_ref[r]
        if x.page != last_page or x.pos < x.page_len - EDGE_LINES:
            problems.append(f"footer의 L{r}이 마지막 쪽의 아래 {EDGE_LINES}줄 안에 있지 않음")
    raw_fields = data["fields"]
    if len(raw_fields) > MAX_FIELDS:
        problems.append(f"항목이 {len(raw_fields)}개. 최대 {MAX_FIELDS}개")
    used = set(header) | set(footer)
    parsed, titles = [], {}
    for i, f in enumerate(raw_fields[:MAX_FIELDS + 1]):
        where = f"fields[{i}]"
        if not isinstance(f, dict):
            problems.append(f"{where}이 객체가 아님")
            continue
        ref, title, kind = f.get("ref"), f.get("title"), f.get("type")
        if isinstance(ref, bool) or not isinstance(ref, int) or ref not in by_ref:
            problems.append(f"{where}의 ref가 없는 줄 번호")
            continue
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE:
            problems.append(f"{where}의 title이 1~{MAX_TITLE}자의 글이 아님")
            continue
        title = title.strip()
        if _compact(title) not in _compact(by_ref[ref].text):
            problems.append(f"{where}의 title이 L{ref}의 글에 없음")
            continue
        if ref in used:
            problems.append(f"L{ref}를 두 곳에 씀")
        used.add(ref)
        if kind not in TYPES:
            problems.append(f"{where}의 type이 {'/'.join(TYPES)} 중 하나가 아님")
            continue
        columns = f.get("columns")
        if kind == "table":
            if isinstance(columns, bool) or not isinstance(columns, int) or not 1 <= columns <= MAX_COLUMNS:
                problems.append(f"{where}는 표인데 columns가 1~{MAX_COLUMNS}의 정수가 아님")
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
    if len(set(refs)) != len(refs) or refs != sorted(refs):
        problems.append("항목은 문서에 나온 순서(ref 오름차순)이고 같은 줄을 두 번 쓰지 않아야 함")
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
            kind = "list"
            warnings.append({"code": CHECKLIST_NO_BOX, "title": p["title"]})
        item = {"title": p["title"], "type": kind, "aiFill": True}
        if kind == "table":
            item["columns"] = p["columns"]
        description = " ".join(by_ref[r].text for r in p["desc"])
        if description:
            if len(description) > MAX_DESCRIPTION:
                return None, [f"{p['title']}의 설명이 {MAX_DESCRIPTION}자를 넘음"]
            item["description"] = description
        out.append(item)
    join = lambda rs: "\n".join(by_ref[r].text for r in rs) or None     # noqa: E731
    return Imported(out, join(header), join(footer), warnings), []


def import_template(data: bytes, llm, timeout_s: float, max_retries: int = 2, call_retries: int = 1,
                    prompt: Prompt | None = None) -> ImportOutcome:
    """양식 PDF -> ImportOutcome. 파일을 읽지 못하면 error_code 에 IngestError 의 코드(no_text 등), AI 쪽은 placement 의
    CHECK_FAILED / LLM_TIMEOUT / LLM_FAILED, 항목이 없으면 NO_FIELDS."""
    try:
        lines = read_lines(data)
    except IngestError as e:
        return ImportOutcome(None, e.code)
    out: Outcome = ask(prompt or load_prompt(None, KIND), lambda problems: build_user(lines, problems),
                       lambda raw: check(raw, lines), llm, timeout_s, max_retries, call_retries)
    if out.placement is None:
        return ImportOutcome(None, out.error_code, out.attempts, out.input_tokens, out.output_tokens)
    if not out.placement.fields:
        return ImportOutcome(None, NO_FIELDS, out.attempts, out.input_tokens, out.output_tokens)
    return ImportOutcome(out.placement, None, out.attempts, out.input_tokens, out.output_tokens)
