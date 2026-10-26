"""초안 생성 요청 검사. 명세 2.1·5장. 틀리면 InvalidInput — API 가 접수 전에 400 INVALID_INPUT 으로 바꾼다.

오류 글에는 칸 위치만 쓰고 값은 쓰지 않는다 (응답·로그에 회의 내용이 새지 않게).
엄격하게: 번호·타입, 칸 이름(1~20개, 1~100자, 중복 없음), 요약의 안건이 agendas 에 있는지, 항목 id 중복, 근거 줄 번호,
          이전 줄(previousLines, 600개·글 2,000자 이하)의 칸 타입과 draftEdited
너그럽게: 모르는 의견 kind(그대로 "의견"으로 보임), 빠진 edited·needsCheck(false), 빠진 summary·opinions·todos(빈 목록),
          보드 본문의 다른 칸(version, offTopic, leftover 등)과 transcripts 등 쓰지 않는 칸,
          이전 줄의 출처가 요약에 없거나 서로 겹치는 것 (명세 8.3: 거절하지 않고 처리 단계에서 다룬다)
"""

from .models import KIND_RANK

MAX_ITEMS = 20
MAX_ITEM_NAME = 100
MAX_REQUEST_ID = 200
MAX_PREVIOUS_LINES = 600
MAX_LINE_TEXT = 2000
STATUSES = ("NOT_STARTED", "DISCUSSING", "DECIDED", "UNDECIDED", "DECISION_CHECK")
KINDS = tuple(KIND_RANK)


class InvalidInput(ValueError):
    """요청 형식 오류. 메시지는 400 응답의 detail 이 된다."""


def validate_request(body) -> None:
    if not isinstance(body, dict):
        raise InvalidInput("본문이 JSON 객체가 아님")
    _text(body.get("requestId"), "requestId", MAX_REQUEST_ID)
    for key in ("minutesId", "meetingId"):
        _positive_int(body.get(key), key)
    _text(body.get("callbackUrl"), "callbackUrl")
    _template(body.get("template"))
    agenda_ids = _agendas(body.get("agendas"))
    _summary(body.get("summary"), agenda_ids)
    _previous_lines(body.get("previousLines"))


def _template(template):
    if not isinstance(template, dict):
        raise InvalidInput("template이 객체가 아님")
    items = template.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise InvalidInput(f"template.items는 1~{MAX_ITEMS}개의 목록이어야 함")
    seen = set()
    for i, name in enumerate(items):
        _text(name, f"template.items[{i}]", MAX_ITEM_NAME)
        if name.strip() in seen:
            raise InvalidInput(f"template.items[{i}]의 칸 이름이 앞의 칸과 겹침")
        seen.add(name.strip())


def _agendas(agendas) -> set:
    if not isinstance(agendas, list):
        raise InvalidInput("agendas가 목록이 아님")
    ids = set()
    for i, a in enumerate(agendas):
        where = f"agendas[{i}]"
        if not isinstance(a, dict):
            raise InvalidInput(f"{where}가 객체가 아님")
        _positive_int(a.get("agendaId"), f"{where}.agendaId")
        if a["agendaId"] in ids:
            raise InvalidInput(f"{where}.agendaId가 앞의 안건과 겹침")
        ids.add(a["agendaId"])
        _text(a.get("title"), f"{where}.title")
        _optional_str(a.get("decisionPoint"), f"{where}.decisionPoint")
    return ids


def _summary(summary, agenda_ids):
    if not isinstance(summary, dict) or not isinstance(summary.get("agendas"), list):
        raise InvalidInput("summary.agendas가 목록이 아님")
    seen_agendas, item_ids = set(), set()
    for i, a in enumerate(summary["agendas"]):
        where = f"summary.agendas[{i}]"
        if not isinstance(a, dict):
            raise InvalidInput(f"{where}가 객체가 아님")
        _positive_int(a.get("agendaId"), f"{where}.agendaId")
        if a["agendaId"] not in agenda_ids:
            raise InvalidInput(f"{where}.agendaId가 agendas에 없음")
        if a["agendaId"] in seen_agendas:
            raise InvalidInput(f"{where}.agendaId가 앞의 안건과 겹침")
        seen_agendas.add(a["agendaId"])
        if a.get("status") not in STATUSES:
            raise InvalidInput(f"{where}.status는 {', '.join(STATUSES)} 중 하나여야 함")
        reason = a.get("statusReason")
        if reason is not None:
            if not isinstance(reason, dict):
                raise InvalidInput(f"{where}.statusReason이 객체가 아님")
            _optional_str(reason.get("text"), f"{where}.statusReason.text")
            _seqs(reason.get("evidenceSeqs", []), f"{where}.statusReason.evidenceSeqs")
        for key, text_key in (("summary", "text"), ("opinions", "text"), ("todos", "task")):
            items = a.get(key) or []
            if not isinstance(items, list):
                raise InvalidInput(f"{where}.{key}가 목록이 아님")
            for j, item in enumerate(items):
                _item(item, f"{where}.{key}[{j}]", text_key, item_ids)


def _previous_lines(lines):
    """다시 배치의 이전 줄 (명세 5.7, 8장). 출처가 요약에 있는지·겹치는지는 검사하지 않는다 (8.3: 잃지 않게 처리)."""
    if lines is None:
        return
    if not isinstance(lines, list) or len(lines) > MAX_PREVIOUS_LINES:
        raise InvalidInput(f"previousLines는 {MAX_PREVIOUS_LINES}개 이하의 목록이어야 함")
    for i, line in enumerate(lines):
        where = f"previousLines[{i}]"
        if not isinstance(line, dict):
            raise InvalidInput(f"{where}가 객체가 아님")
        _text(line.get("title"), f"{where}.title", MAX_ITEM_NAME)
        _text(line.get("sentence"), f"{where}.sentence", MAX_LINE_TEXT)
        _seqs(line.get("transcriptSeqs"), f"{where}.transcriptSeqs")
        if not isinstance(line.get("draftEdited"), bool):
            raise InvalidInput(f"{where}.draftEdited가 true/false가 아님")
        for flag in ("edited", "needsCheck"):
            if flag in line and not isinstance(line[flag], bool):
                raise InvalidInput(f"{where}.{flag}가 true/false가 아님")
        sources = line.get("sources")
        if not isinstance(sources, list):
            raise InvalidInput(f"{where}.sources가 목록이 아님")
        for j, s in enumerate(sources):
            _source(s, f"{where}.sources[{j}]")


def _source(source, where):
    if not isinstance(source, dict):
        raise InvalidInput(f"{where}가 객체가 아님")
    _positive_int(source.get("agendaId"), f"{where}.agendaId")
    if source.get("kind") not in KINDS:
        raise InvalidInput(f"{where}.kind는 {', '.join(KINDS)} 중 하나여야 함")
    _optional_str(source.get("id"), f"{where}.id")


def _item(item, where, text_key, item_ids):
    if not isinstance(item, dict):
        raise InvalidInput(f"{where}가 객체가 아님")
    _text(item.get("id"), f"{where}.id")
    if item["id"] in item_ids:
        raise InvalidInput(f"{where}.id가 앞의 항목과 겹침")
    item_ids.add(item["id"])
    _text(item.get(text_key), f"{where}.{text_key}")
    _seqs(item.get("evidenceSeqs"), f"{where}.evidenceSeqs")
    for flag in ("edited", "needsCheck", "unclear"):
        if flag in item and not isinstance(item[flag], bool):
            raise InvalidInput(f"{where}.{flag}가 true/false가 아님")
    for key in ("kind", "assigneeName", "dueText"):
        _optional_str(item.get(key), f"{where}.{key}")


def _seqs(value, where):
    if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in value):
        raise InvalidInput(f"{where}는 0 이상 정수의 목록이어야 함")


def _positive_int(value, where):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidInput(f"{where}는 양의 정수여야 함")


def _text(value, where, max_len=None):
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{where}가 비었거나 문자열이 아님")
    if max_len and len(value) > max_len:
        raise InvalidInput(f"{where}는 {max_len}자 이하여야 함")


def _optional_str(value, where):
    if value is not None and not isinstance(value, str):
        raise InvalidInput(f"{where}는 문자열이나 null이어야 함")
