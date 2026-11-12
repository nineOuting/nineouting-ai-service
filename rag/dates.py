"""근거에 붙이는 날짜 글 (SCRUM-234). AI 가 근거 중 어느 쪽이 최근인지 알 수 있게 조각 머리에 붙인다.

  회의록      "회의 2026년 10월 29일 10:00"   meetingStartAt 그대로. 0시 정각이면 날짜만 (날짜만 받은 임시 형식)
  첨부 자료   "올린 날 2026년 10월 6일"        AI 서버가 그 버전을 받은 시각을 한국 시간으로. 문서를 쓴 날이 아니다
  날짜가 없으면 빈 글 (붙이지 않는다). 예: 날짜를 저장하기 전에 넣은 회의록
"""
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def when_label(source_type: str, meeting_started_at: datetime | None, received_at: datetime | None) -> str:
    if source_type == "minutes":
        if meeting_started_at is None:
            return ""
        t = meeting_started_at
        day = f"{t.year}년 {t.month}월 {t.day}일"
        return f"회의 {day}" if (t.hour, t.minute, t.second) == (0, 0, 0) else f"회의 {day} {t:%H:%M}"
    if received_at is None:
        return ""
    t = received_at.astimezone(KST) if received_at.tzinfo else received_at
    return f"올린 날 {t.year}년 {t.month}월 {t.day}일"
