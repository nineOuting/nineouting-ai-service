"""근거 날짜 시험 (SCRUM-234). DB 없이."""
from datetime import datetime, timedelta, timezone

import pytest

from rag.dates import now_label, when_label
from rag.loaders.minutes import meeting_time


@pytest.mark.parametrize("raw, want", [
    ("2026-10-29T10:00:00", datetime(2026, 10, 29, 10, 0)),
    ("2026-10-29T10:00:00+09:00", datetime(2026, 10, 29, 10, 0)),      # 시간대는 떼고 벽시계 시각 그대로
    ("2026-10-29T01:00:00Z", datetime(2026, 10, 29, 1, 0)),            # 바꾸지 않는다
    ("2026-10-29", datetime(2026, 10, 29)),
    ("  2026-10-29T10:00  ", datetime(2026, 10, 29, 10, 0)),
    ("어제", None), ("", None),
])
def test_확정_회의록의_회의_시작_시각(raw, want):
    assert meeting_time({"formatVersion": "1.0", "meetingStartAt": raw}) == want


def test_임시_형식은_date_를_읽고_없으면_None():
    assert meeting_time({"schema_version": "0.1", "date": "2026-10-20"}) == datetime(2026, 10, 20)
    assert meeting_time({"schema_version": "0.1"}) is None
    assert meeting_time({"formatVersion": "1.0", "meetingStartAt": 3}) is None


def test_회의록은_회의한_날_시각까지_0시면_날짜만():
    assert when_label("minutes", datetime(2026, 10, 29, 10, 5), None) == "회의 2026년 10월 29일 10:05"
    assert when_label("minutes", datetime(2026, 10, 20), None) == "회의 2026년 10월 20일"
    assert when_label("minutes", None, datetime(2026, 10, 1, tzinfo=timezone.utc)) == ""   # 받은 시각은 쓰지 않음


def test_첨부_자료는_받은_시각을_한국_날짜로():
    utc_late = datetime(2026, 10, 5, 16, 30, tzinfo=timezone.utc)                     # 한국 시각 10월 6일 01:30
    assert when_label("upload", None, utc_late) == "올린 날 2026년 10월 6일"
    kst = datetime(2026, 10, 6, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    assert when_label("upload", None, kst) == "올린 날 2026년 10월 6일"
    assert when_label("upload", None, None) == ""


def test_지금_칸_글():
    from datetime import datetime, timezone
    from rag.dates import KST
    assert now_label(datetime(2026, 11, 12, 15, 40, tzinfo=KST)) == "2026년 11월 12일 (목) 15:40"
    assert now_label(datetime(2026, 11, 12, 6, 5, tzinfo=timezone.utc)) == "2026년 11월 12일 (목) 15:05"    # 한국 시간으로
    assert now_label(datetime(2026, 11, 15, 9, 0)) == "2026년 11월 15일 (일) 09:00"                          # 시간대 없으면 그대로
