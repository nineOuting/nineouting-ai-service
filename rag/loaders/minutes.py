"""회의록 JSON -> Piece 목록. 두 형식을 받는다 (회의록 초안 명세 docs/api/minutes-draft-contract.md 4.3).

  formatVersion "1.0"   확정 회의록 (명세 4.1). 칸(title, content)의 줄 하나가 Piece 하나, 근거 위치는 칸 이름
  schema_version "0.1"  임시 형식 (아래). 백엔드가 확정 회의록 형식으로 부르기 시작하면 CN팀과 맞춰 없앤다

확정 회의록 규칙
  - content 를 줄로 나누고 줄 앞 표시("- ", "* ", "• ")를 뗀다. 빈 줄과 "해당 내용 없음" 줄은 넣지 않는다
  - content 안의 마크다운 표("|" 로 시작하는 줄이 이어지고 둘째 줄이 |---| 구분 줄)는 머리 행 아래의 행 하나가 Piece 하나.
    행은 "열 이름: 값 | …" (빈 칸은 뺌, 워드·엑셀 표와 같은 규칙). 몸통 행이 없으면 머리 행 이름들을 한 줄로.
    칸 안의 "\\|" 는 칸 구분이 아니다. 구분 줄이 없는 "|" 줄은 표가 아니라 보고 줄 그대로 넣는다
  - 칸 이름을 맥락으로 붙여 검색에 쓴다 ("[결정 사항] …"). 사용자가 추가한 칸도 같다
  - location = {"type": "section", "heading": 칸 이름, "label": 칸 이름}  (워드 문서의 제목 위치와 같은 모양)
  - 머리글·바닥글(header, footer)은 글이 있으면 각각 Piece 하나 (여러 줄이면 공백으로 이음).
    heading·label·맥락은 "머리글"·"바닥글"
  - 넣을 줄이 없으면 no_text, 형식이 틀리면 internal_error (접수 단계에서 check_format 으로 먼저 400)

임시 입력 형식은 summary/meeting_summary.py 가 쓰는 summary.json v0.1 이다.
  {"schema_version": "0.1", "meeting_id": ..., "date": ...,
   "summary": [{"text": ..., "topic": ..., "evidence": [{"start": 초, "end": 초}, ...]}],
   "decisions": [...], "action_items": [...]}
decisions·action_items 항목은 아직 형식이 정해지지 않아 summary 항목과 같은 모양이라고 가정한다.

규칙
  - 항목 하나가 Piece 하나가 된다
  - evidence 의 초(소수)를 밀리초 정수로 바꾼다
  - 근거 구간이 여러 개면 startMs~endMs 는 전체를 덮는 범위, label 은 첫 구간 + "외 N곳",
    원래 구간은 location.spans 에 둔다
  - 글이 비었거나 근거가 없는 항목은 건너뛴다 (근거 없는 조각은 citation 을 만들 수 없다)
  - 넣을 항목이 없으면 no_text, 형식이 틀리면 internal_error
"""
import math
import re
from dataclasses import dataclass

from ..errors import INTERNAL_ERROR, NO_TEXT, IngestError
from ..models import Piece
from .text import header_line, row_line

SCHEMA_VERSION = "0.1"
FORMAT_VERSION = "1.0"
SECTIONS = ("summary", "decisions", "action_items")
EMPTY_SECTION = "해당 내용 없음"           # 회의록 초안이 빈 칸에 쓰는 글 (minutes/lines.py)
_BULLET = re.compile(r"^(?:[-*•]\s+)")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")     # |---|:---:| 같은 구분 줄
_CELL_SPLIT = re.compile(r"(?<!\\)\|")                                         # \| 는 칸 안의 글
EDGES = (("header", "머리글"), ("footer", "바닥글"))


@dataclass(frozen=True)
class LoadResult:
    pieces: list[Piece]
    skipped: int                 # 글이 없거나 근거가 없어 건너뛴 항목 수


def load_minutes(minutes) -> LoadResult:
    if check_format(minutes) == FORMAT_VERSION:
        return _load_confirmed(minutes)
    return _load_temporary(minutes)


def check_format(minutes) -> str:
    """형식 이름(FORMAT_VERSION 또는 SCHEMA_VERSION)을 돌려준다. 틀리면 IngestError(INTERNAL_ERROR).
    확정 회의록은 모양까지 본다. 임시 형식은 버전만 보고 내용은 읽을 때 본다 (지금까지와 같음).
    오류 글에는 칸 위치만 쓰고 회의록 글은 쓰지 않는다."""
    if not isinstance(minutes, dict):
        raise _invalid("회의록이 JSON 객체가 아님")
    if minutes.get("formatVersion") == FORMAT_VERSION:
        _check_confirmed(minutes)
        return FORMAT_VERSION
    if minutes.get("schema_version") == SCHEMA_VERSION:
        return SCHEMA_VERSION
    raise _invalid(f"formatVersion 이 {FORMAT_VERSION} 도, schema_version 이 {SCHEMA_VERSION} 도 아님")


def _check_confirmed(minutes):
    meeting_id = minutes.get("meetingId")
    if isinstance(meeting_id, bool) or not isinstance(meeting_id, int) or meeting_id <= 0:
        raise _invalid("meetingId 가 양의 정수가 아님")
    if not isinstance(minutes.get("meetingStartAt"), str) or not minutes["meetingStartAt"].strip():
        raise _invalid("meetingStartAt 이 비었거나 문자열이 아님")
    for key in ("confirmedAt", "savedAt", "header", "footer"):
        if minutes.get(key) is not None and not isinstance(minutes[key], str):
            raise _invalid(f"{key} 가 문자열이 아님")
    sections = minutes.get("sections")
    if not isinstance(sections, list):
        raise _invalid("sections 가 배열이 아님")
    for i, sec in enumerate(sections):
        if not isinstance(sec, dict):
            raise _invalid(f"sections[{i}] 가 객체가 아님")
        if not isinstance(sec.get("title"), str) or not sec["title"].strip():
            raise _invalid(f"sections[{i}].title 이 비었거나 문자열이 아님")
        if not isinstance(sec.get("content"), str):
            raise _invalid(f"sections[{i}].content 가 문자열이 아님")


def _load_confirmed(minutes) -> LoadResult:
    pieces, skipped = [], 0
    for key, name in EDGES[:1]:
        pieces += _edge_piece(minutes.get(key), name)
    for sec in minutes["sections"]:
        title = sec["title"].strip()
        location = {"type": "section", "heading": title, "label": title}
        for line in section_lines(sec["content"]):
            if line == EMPTY_SECTION:
                skipped += 1
                continue
            pieces.append(Piece(content=line, location=dict(location), context=title, kind="section"))
    for key, name in EDGES[1:]:
        pieces += _edge_piece(minutes.get(key), name)
    if not pieces:
        raise IngestError(NO_TEXT, f"넣을 줄 없음 (해당 내용 없음 {skipped}개)")
    return LoadResult(pieces, skipped)


def section_lines(content: str) -> list[str]:
    """칸 글 -> 넣을 줄 목록. 일반 줄은 줄 앞 표시를 떼고, 마크다운 표는 행마다 "열 이름: 값 | …" 한 줄."""
    raw = [line.strip() for line in content.splitlines()]
    out, i = [], 0
    while i < len(raw):
        if raw[i].startswith("|") and i + 1 < len(raw) and _TABLE_RULE.match(raw[i + 1]):
            end = i + 2
            while end < len(raw) and raw[end].startswith("|"):
                end += 1
            out += _table_lines(raw[i], raw[i + 2:end])
            i = end
            continue
        line = _BULLET.sub("", raw[i]).strip()
        if line:
            out.append(line)
        i += 1
    return out


def _table_lines(head: str, body: list[str]) -> list[str]:
    header = _cells(head)
    lines = [line for line in (row_line(header, _cells(row)) for row in body) if line]
    if not lines and header_line(header):
        lines = [header_line(header)]
    return lines


def _cells(row: str) -> list[str]:
    """"| a | b \\| c |" -> ["a", "b | c"]. 양 끝의 | 는 있어도 없어도 된다."""
    inner = row.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") and not inner.endswith("\\|") else inner
    return [c.strip().replace("\\|", "|") for c in _CELL_SPLIT.split(inner)]


def _edge_piece(text, name) -> list[Piece]:
    """머리글·바닥글. 글이 있으면 Piece 하나."""
    joined = " ".join(line.strip() for line in (text or "").splitlines() if line.strip())
    if not joined:
        return []
    return [Piece(content=joined, location={"type": "section", "heading": name, "label": name}, context=name,
                  kind="section")]


def _load_temporary(minutes) -> LoadResult:
    if not isinstance(minutes.get("summary"), list):
        raise _invalid("summary 가 배열이 아님")

    pieces, skipped = [], 0
    for kind in SECTIONS:
        items = minutes.get(kind)
        if items is None:
            continue
        if not isinstance(items, list):
            raise _invalid(f"{kind} 가 배열이 아님")
        for i, item in enumerate(items):
            where = f"{kind}[{i}]"
            if not isinstance(item, dict):
                raise _invalid(f"{where} 가 객체가 아님")
            text = _optional_str(item, "text", where)
            topic = _optional_str(item, "topic", where)
            spans = _spans(item.get("evidence"), where)
            if not text or not spans:
                skipped += 1
                continue
            pieces.append(Piece(content=text, location=_location(spans), context=topic, kind=kind))

    if not pieces:
        raise IngestError(NO_TEXT, f"넣을 항목 없음 (건너뜀 {skipped}개)")
    return LoadResult(pieces, skipped)


def _invalid(detail):
    return IngestError(INTERNAL_ERROR, f"회의록 형식 오류: {detail}")


def _optional_str(item, key, where):
    """문자열이면 앞뒤 공백을 지워 돌려주고, 없거나 비었으면 None."""
    v = item.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise _invalid(f"{where}.{key} 가 문자열이 아님")
    return v.strip() or None


def _spans(evidence, where):
    """evidence -> [(startMs, endMs), ...] 시작 순 정렬. 없으면 빈 목록."""
    if evidence is None:
        return []
    if not isinstance(evidence, list):
        raise _invalid(f"{where}.evidence 가 배열이 아님")
    spans = []
    for j, ev in enumerate(evidence):
        at = f"{where}.evidence[{j}]"
        if not isinstance(ev, dict):
            raise _invalid(f"{at} 가 객체가 아님")
        start, end = _seconds(ev.get("start"), f"{at}.start"), _seconds(ev.get("end"), f"{at}.end")
        if start > end:
            raise _invalid(f"{at} 의 start 가 end 보다 큼")
        spans.append((round(start * 1000), round(end * 1000)))
    return sorted(spans)


def _seconds(v, at):
    # bool 은 int 의 하위 형이라 따로 막는다
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise _invalid(f"{at} 가 0 이상의 숫자가 아님")
    return v


def _location(spans):
    start, end = spans[0][0], max(b for _, b in spans)
    with_hour = end >= 3_600_000
    label = f"{_clock(spans[0][0], with_hour)}~{_clock(spans[0][1], with_hour)}"
    loc = {"type": "time", "startMs": start, "endMs": end, "label": label}
    if len(spans) > 1:
        loc["label"] = f"{label} 외 {len(spans) - 1}곳"
        loc["spans"] = [{"startMs": a, "endMs": b} for a, b in spans]
    return loc


def _clock(ms, with_hour):
    s = ms // 1000
    h, m, sec = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if with_hour else f"{m:02d}:{sec:02d}"
