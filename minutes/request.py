"""초안 생성 요청 검사. 명세 2.1·5장. 틀리면 InvalidInput — API 가 접수 전에 400 INVALID_INPUT 으로 바꾼다.

오류 글에는 칸 위치만 쓰고 값은 쓰지 않는다 (응답·로그에 회의 내용이 새지 않게).
엄격하게: 번호·타입, 양식 필드(1~20개, 이름 1~100자·중복 없음, 종류, AI 채움 설정, 글 20,000자, 설명 2,000자, 표 열 개수 1~6), 요약의 안건이 agendas 에 있는지,
          항목 id 중복, 근거 줄 번호, 결정할 사항(안건 목록 1개 이상·번호 중복 없음, 요약의 사항별 결과는 필수),
          이전 줄(previousLines, 600개·글 2,000자 이하)의 칸 타입과 draftEdited
너그럽게: 모르는 의견 kind(그대로 "의견"으로 보임), 빠진 edited·needsCheck(false), 빠진 summary·opinions·todos(빈 목록),
          모르는 사항 상태(줄을 만들지 않음), 안건의 statusReason(읽지 않음. 명세 v0.8),
          전환 기간의 agendas[].decisionPoint(글 하나 → 번호 없는 사항 1개), 결정할 사항이 없는 안건 (v0.7 에서 선택이었으므로),
          보드 본문의 다른 칸(version, offTopic, leftover 등)과 transcripts 등 쓰지 않는 칸,
          이전 줄의 출처가 요약에 없거나 서로 겹치는 것 (명세 8.3: 거절하지 않고 처리 단계에서 다룬다)
"""

from .models import KIND_RANK, TABLE, TEXT, Field

MAX_ITEMS = 20
MAX_ITEM_NAME = 100
MAX_USER_TEXT = 20_000                  # 사용자가 채우는 필드의 글, 머리글·바닥글 (명세 7.5)
MAX_DESCRIPTION = 2000                  # 필드 설명 (명세 7.5)
MAX_TABLE_COLUMNS = 6                   # 표 필드의 열 개수 (명세 5.8). 글 필드의 columns 는 읽지 않는다
FIELD_TYPES = (TEXT, TABLE)
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


def template_fields(template: dict) -> tuple[list[Field], bool]:
    """(필드 목록, v0.7 모양인가). 검사를 통과한 양식만 받는다.
    v0.7 은 template.fields, v0.6 은 template.items(이름 목록 → 모두 글·AI 채움). 둘 다 있으면 fields."""
    if "fields" in template:
        return [Field(f["title"], f["type"], f["aiFill"], f.get("content") or "", f.get("description") or "",
                      f.get("columns") if f["type"] == TABLE else None)
                for f in template["fields"]], True
    return [Field(name) for name in template["items"]], False


def _template(template):
    if not isinstance(template, dict):
        raise InvalidInput("template이 객체가 아님")
    if "fields" in template:
        _fields(template["fields"])
        for key in ("header", "footer"):
            _optional_str(template.get(key), f"template.{key}")
            if template.get(key) and len(template[key]) > MAX_USER_TEXT:
                raise InvalidInput(f"template.{key}는 {MAX_USER_TEXT}자 이하여야 함")
        return
    items = template.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise InvalidInput(f"template.items는 1~{MAX_ITEMS}개의 목록이어야 함")
    seen = set()
    for i, name in enumerate(items):
        _text(name, f"template.items[{i}]", MAX_ITEM_NAME)
        if name.strip() in seen:
            raise InvalidInput(f"template.items[{i}]의 칸 이름이 앞의 칸과 겹침")
        seen.add(name.strip())


def _fields(fields):
    if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_ITEMS:
        raise InvalidInput(f"template.fields는 1~{MAX_ITEMS}개의 목록이어야 함")
    seen = set()
    for i, f in enumerate(fields):
        where = f"template.fields[{i}]"
        if not isinstance(f, dict):
            raise InvalidInput(f"{where}가 객체가 아님")
        _text(f.get("title"), f"{where}.title", MAX_ITEM_NAME)
        if f["title"].strip() in seen:
            raise InvalidInput(f"{where}.title이 앞의 필드와 겹침")
        seen.add(f["title"].strip())
        if f.get("type") not in FIELD_TYPES:
            raise InvalidInput(f"{where}.type은 {', '.join(FIELD_TYPES)} 중 하나여야 함")
        if not isinstance(f.get("aiFill"), bool):
            raise InvalidInput(f"{where}.aiFill이 true/false가 아님")
        columns = f.get("columns")
        if f["type"] == TABLE and columns is not None and (
                isinstance(columns, bool) or not isinstance(columns, int) or not 1 <= columns <= MAX_TABLE_COLUMNS):
            raise InvalidInput(f"{where}.columns는 1~{MAX_TABLE_COLUMNS} 사이의 정수여야 함")
        _optional_str(f.get("description"), f"{where}.description")
        if f.get("description") and len(f["description"]) > MAX_DESCRIPTION:
            raise InvalidInput(f"{where}.description은 {MAX_DESCRIPTION}자 이하여야 함")
        _optional_str(f.get("content"), f"{where}.content")
        if f.get("content") and len(f["content"]) > MAX_USER_TEXT:
            raise InvalidInput(f"{where}.content는 {MAX_USER_TEXT}자 이하여야 함")


def _agendas(agendas) -> set:
    if not isinstance(agendas, list):
        raise InvalidInput("agendas가 목록이 아님")
    ids, point_ids = set(), set()
    for i, a in enumerate(agendas):
        where = f"agendas[{i}]"
        if not isinstance(a, dict):
            raise InvalidInput(f"{where}가 객체가 아님")
        _positive_int(a.get("agendaId"), f"{where}.agendaId")
        if a["agendaId"] in ids:
            raise InvalidInput(f"{where}.agendaId가 앞의 안건과 겹침")
        ids.add(a["agendaId"])
        _text(a.get("title"), f"{where}.title")
        _optional_str(a.get("decisionPoint"), f"{where}.decisionPoint")       # 전환 기간 (명세 5.1)
        if "decisionPoints" in a:
            _agenda_points(a["decisionPoints"], f"{where}.decisionPoints", point_ids)
    return ids


def _agenda_points(points, where, seen):
    """안건 목록의 결정할 사항 (명세 5.1). 1개 이상, 번호는 회의 안에서 겹치지 않는다."""
    if not isinstance(points, list) or not points:
        raise InvalidInput(f"{where}는 1개 이상의 목록이어야 함")
    for j, p in enumerate(points):
        at = f"{where}[{j}]"
        if not isinstance(p, dict):
            raise InvalidInput(f"{at}가 객체가 아님")
        _point_id(p.get("decisionPointId"), f"{at}.decisionPointId", allow_null=False)
        if p["decisionPointId"] in seen:
            raise InvalidInput(f"{at}.decisionPointId가 앞의 사항과 겹침")
        seen.add(p["decisionPointId"])
        if isinstance(p.get("position"), bool) or not isinstance(p.get("position"), int):
            raise InvalidInput(f"{at}.position이 정수가 아님")
        _text(p.get("text"), f"{at}.text")


def _summary(summary, agenda_ids):
    if not isinstance(summary, dict) or not isinstance(summary.get("agendas"), list):
        raise InvalidInput("summary.agendas가 목록이 아님")
    seen_agendas, item_ids, point_ids = set(), set(), set()
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
        _result_points(a.get("decisionPoints"), f"{where}.decisionPoints", point_ids)
        for key, text_key in (("summary", "text"), ("opinions", "text"), ("todos", "task")):
            items = a.get(key) or []
            if not isinstance(items, list):
                raise InvalidInput(f"{where}.{key}가 목록이 아님")
            for j, item in enumerate(items):
                _item(item, f"{where}.{key}[{j}]", text_key, item_ids)


def _result_points(points, where, seen):
    """요약의 결정할 사항별 판정 결과 (명세 5.2 DecisionPointResult). 결정·미결 줄의 재료라 필수다.
    없으면 v0.7 까지의 요약(안건 단위 판정)이라 결정 줄을 만들 수 없으므로 거절한다."""
    if not isinstance(points, list):
        raise InvalidInput(f"{where}가 목록이 아님 (명세 v0.8: 결정할 사항마다의 판정 결과)")
    for j, p in enumerate(points):
        at = f"{where}[{j}]"
        if not isinstance(p, dict):
            raise InvalidInput(f"{at}가 객체가 아님")
        _point_id(p.get("decisionPointId"), f"{at}.decisionPointId", allow_null=True)
        if p["decisionPointId"] is not None:
            if p["decisionPointId"] in seen:
                raise InvalidInput(f"{at}.decisionPointId가 앞의 사항과 겹침")
            seen.add(p["decisionPointId"])
        _text(p.get("status"), f"{at}.status")
        _text(p.get("decisionText"), f"{at}.decisionText")
        _seqs(p.get("evidenceSeqs"), f"{at}.evidenceSeqs")
        for flag in ("edited", "needsCheck"):
            if flag in p and not isinstance(p[flag], bool):
                raise InvalidInput(f"{at}.{flag}가 true/false가 아님")


def _point_id(value, where, allow_null):
    """결정할 사항 번호: 양의 정수나 빈 글이 아닌 문자열 (명세 5.1). 요약 쪽은 전환 기간의 번호 없는 사항이라 null 도 받는다."""
    if value is None and allow_null:
        return
    if isinstance(value, bool) or not (isinstance(value, int) and value > 0 or isinstance(value, str) and value.strip()):
        raise InvalidInput(f"{where}는 양의 정수나 문자열이어야 함")


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
    _point_id(source.get("decisionPointId"), f"{where}.decisionPointId", allow_null=True)   # v0.7 줄에는 없다


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
