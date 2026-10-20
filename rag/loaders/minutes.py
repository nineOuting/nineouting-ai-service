"""확정 회의록 JSON -> Piece 목록.

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
from dataclasses import dataclass

from ..errors import INTERNAL_ERROR, NO_TEXT, IngestError
from ..models import Piece

SCHEMA_VERSION = "0.1"
SECTIONS = ("summary", "decisions", "action_items")


@dataclass(frozen=True)
class LoadResult:
    pieces: list[Piece]
    skipped: int                 # 글이 없거나 근거가 없어 건너뛴 항목 수


def load_minutes(minutes) -> LoadResult:
    if not isinstance(minutes, dict):
        raise _invalid("회의록이 JSON 객체가 아님")
    if minutes.get("schema_version") != SCHEMA_VERSION:
        raise _invalid(f"schema_version 이 {SCHEMA_VERSION} 이 아님")
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
