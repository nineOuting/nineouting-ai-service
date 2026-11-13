"""진행 중 회의 → 챗봇 근거 (SCRUM-224). DB·AI 없음. 글은 합성."""
from rag import live
from rag.live import (TAG_CURRENT, TAG_MINE, TAG_SUMMARY, TAG_UNCLEAR, board_items, line_bundles, pick_board,
                      pick_lines, pick_summary, summary_embed_text, summary_items)


def agenda(aid, title, decisions=(), summary=(), opinions=(), todos=()):
    return {"agendaId": aid, "title": title, "status": "DISCUSSING", "decisionPoints": list(decisions),
            "summary": list(summary), "opinions": list(opinions), "todos": list(todos)}


DESIGN = agenda(
    102, "디자인 시안 수정",
    decisions=[{"decisionPointId": 9, "status": "DECIDED", "decisionText": "시안은 다음 주 수요일까지 다시 보기로 함",
                "evidenceSeqs": [30, 31]},
               {"decisionPointId": 10, "status": "NOT_STARTED", "decisionText": "논의 전", "evidenceSeqs": []},
               {"decisionPointId": 11, "status": "DECISION_CHECK", "decisionText": "버튼 색은 파랑으로", "evidenceSeqs": [33]}],
    summary=[{"id": "s1", "text": "색상 대비가 낮다는 지적이 나옴", "evidenceSeqs": [25, 26]}],
    opinions=[{"id": "o1", "text": "버튼 크기를 키우자", "kind": "OPINION", "evidenceSeqs": [27]},
              {"id": "o2", "text": "일정이 빠듯함", "kind": "CONCERN", "evidenceSeqs": [28]}],
    todos=[{"id": "t1", "task": "시안 색상 대비 고치기", "assigneeName": "박서준", "assigneeStatus": "MATCHED",
            "assigneeUserId": 57, "dueText": "다음 주 수요일까지", "evidenceSeqs": [30], "unclear": False},
           {"id": "t2", "task": "아이콘 다시 그리기", "assigneeName": None, "assigneeStatus": "UNASSIGNED",
            "assigneeUserId": None, "dueText": None, "evidenceSeqs": [34], "unclear": True}])
LAUNCH = agenda(
    101, "출시 일정",
    decisions=[{"decisionPointId": 1, "status": "UNDECIDED", "decisionText": "정식 출시일", "evidenceSeqs": [5]}],
    todos=[{"id": "t9", "task": "스토어 심사 일정 확인", "assigneeName": "김하늘", "assigneeStatus": "MATCHED",
            "assigneeUserId": "42", "dueText": "금요일", "evidenceSeqs": [6]}])


def texts(items):
    return [i.text for i in items]


# ---- 안건 보드 ----

def test_항목마다_근거_하나이고_종류와_안건_제목이_붙음():
    items = board_items([DESIGN])
    assert texts(items) == [
        "디자인 시안 수정 / 결정: 시안은 다음 주 수요일까지 다시 보기로 함",
        "디자인 시안 수정 / 결정 확인 필요: 버튼 색은 파랑으로",
        "디자인 시안 수정 / 요약: 색상 대비가 낮다는 지적이 나옴",
        "디자인 시안 수정 / 의견: 버튼 크기를 키우자",
        "디자인 시안 수정 / 우려: 일정이 빠듯함",
        "디자인 시안 수정 / 할 일: 시안 색상 대비 고치기 (담당: 박서준, 기한: 다음 주 수요일까지)",
        "디자인 시안 수정 / 할 일: 아이콘 다시 그리기 (담당: 정하지 않음)",
    ]                                                     # 논의 전(NOT_STARTED) 사항은 빠짐


def test_근거_위치는_명세의_agenda_모양():
    first = board_items([DESIGN])[0]
    assert first.location == {"type": "agenda", "agendaId": 102, "item": "decision", "itemId": 9,
                              "evidenceSeqs": [30, 31], "label": "디자인 시안 수정 안건의 결정"}
    todo = board_items([DESIGN])[-2]
    assert todo.location["item"] == "todo" and todo.location["itemId"] == "t1"
    assert todo.location["label"] == "디자인 시안 수정 안건의 할 일"


def test_지금_안건이_맨_앞이고_표시가_붙음():
    items = board_items([LAUNCH, DESIGN], current_agenda_id=102)
    assert items[0].text.startswith("디자인 시안 수정") and items[-1].text.startswith("출시 일정")
    assert all(TAG_CURRENT in i.tags for i in items if i.text.startswith("디자인"))
    assert all(TAG_CURRENT not in i.tags for i in items if i.text.startswith("출시"))


def test_지금_안건_번호는_정수와_문자열을_같이_봄():
    assert TAG_CURRENT in board_items([DESIGN], current_agenda_id="102")[0].tags


def test_질문한_사용자의_할_일과_확인_필요_표시():
    items = board_items([DESIGN, LAUNCH], user_id=42)
    tags = {i.location["itemId"]: i.tags for i in items if i.location["item"] == "todo"}
    assert tags == {"t1": (), "t2": (TAG_UNCLEAR,), "t9": (TAG_MINE,)}     # "42" 와 42 는 같은 사용자
    assert TAG_MINE in [i for i in board_items([DESIGN], user_id=57) if i.location["itemId"] == "t1"][0].tags


def test_빈_글과_모르는_상태는_넘기지_않고_모르는_의견_종류는_의견():
    a = agenda(1, "기타",
               decisions=[{"decisionPointId": 1, "status": "SOMETHING_NEW", "decisionText": "새 상태"},
                          {"decisionPointId": 2, "status": "DECIDED", "decisionText": "  "}],
               summary=[{"id": "s", "text": ""}],
               opinions=[{"id": "o", "text": "새 종류 의견", "kind": "NEW_KIND"}],
               todos=[{"id": "t", "task": ""}])
    assert texts(board_items([a])) == ["기타 / 의견: 새 종류 의견"]


def test_칸이_없어도_됨():
    assert board_items([{"agendaId": 1, "title": "빈 안건"}]) == []
    assert board_items([]) == []


# ---- 보드 고르기 ----

def test_상한_안이면_모두():
    items = board_items([DESIGN, LAUNCH])
    assert pick_board(items, max_chars=10_000) == items


def test_넘치면_지금_안건_결정_할_일_요약_의견_순서로_고르고_넘기는_순서는_유지():
    items = board_items([LAUNCH, DESIGN], current_agenda_id=101)     # 출시 일정이 지금 안건
    current = [i for i in items if TAG_CURRENT in i.tags]
    other_decisions = [i for i in items if TAG_CURRENT not in i.tags and i.location["item"] == "decision"]
    budget = sum(len(i.text) for i in current + other_decisions)
    picked = pick_board(items, max_chars=budget)
    assert picked == [i for i in items if i in current + other_decisions]
    one_more = [i for i in items if TAG_CURRENT not in i.tags and i.location["item"] == "todo"][0]
    picked = pick_board(items, max_chars=budget + len(one_more.text))
    assert one_more in picked and not any(i.location["item"] in ("summary", "opinion") for i in picked)


def test_넘치는_항목은_건너뛰고_다음_것을_봄():
    long = agenda(1, "가", decisions=[{"decisionPointId": 1, "status": "DECIDED", "decisionText": "나" * 100}],
                  todos=[{"id": "t", "task": "짧은 일"}])
    items = board_items([long])
    assert texts(pick_board(items, max_chars=50)) == ["가 / 할 일: 짧은 일 (담당: 정하지 않음)"]


# ---- 전사 줄 ----

def line(seq, text, start=None, end=None, kind=None, low=False):
    start = seq * 1000 if start is None else start
    return {"seq": seq, "startMs": start, "endMs": end, "text": text, "kind": kind, "lowConfidence": low}


def test_맞장구_간투사_확신도_낮은_줄은_빼고_5줄씩_묶음():
    lines = [line(1, "네네", kind="backchannel"), line(2, "음", kind="filler"), line(3, "잘 안 들림", low=True)]
    lines += [line(s, f"발언 {s}", end=s * 1000 + 500) for s in range(4, 11)]          # 7줄
    b = line_bundles(lines)
    assert texts(b) == ["발언 4\n발언 5\n발언 6\n발언 7\n발언 8", "발언 9\n발언 10"]
    assert b[0].location == {"type": "time", "seq": 4, "startMs": 4000, "endMs": 8500, "label": "0:04"}
    assert b[0].tags == ()


def test_400자를_넘으면_새_묶음이고_긴_줄은_혼자_묶임():
    lines = [line(1, "가" * 300), line(2, "나" * 150), line(3, "다" * 500), line(4, "라")]
    assert [len(t) for t in texts(line_bundles(lines))] == [300, 150, 500, 1]


def test_endMs가_없으면_startMs():
    b = line_bundles([line(31, "시안은 수요일까지", start=1_820_000)])
    assert b[0].location["endMs"] == 1_820_000 and b[0].location["label"] == "30:20"


def test_한_시간이_넘으면_시까지():
    assert line_bundles([line(1, "가", start=3_725_000)])[0].location["label"] == "1:02:05"


def test_전사_줄은_가장_최근_묶음부터_상한까지_시간_순서로():
    b = line_bundles([line(s, "가" * 100) for s in range(1, 21)], max_lines=1)       # 100자 묶음 20개
    picked = pick_lines(b, max_chars=350)
    assert [p.location["seq"] for p in picked] == [18, 19, 20]
    assert pick_lines(b, max_chars=10_000) == b
    assert pick_lines([], max_chars=10) == []


def test_정해_둔_값():
    assert (live.BOARD_MAX_CHARS, live.LINES_MAX_CHARS, live.BUNDLE_LINES, live.BUNDLE_CHARS) == (6000, 4000, 5, 400)
    assert (live.SUMMARY_MAX_CHARS, live.SUMMARY_EMBED_CHARS) == (6000, 300)


# ---- 실시간 요약 (SCRUM-249) ----

def summ(text, topic=None, start=None, end=None):
    ev = [] if start is None else [{"start": start, "end": end}]
    return {"text": text, "topic": topic, "evidence": ev}


def test_요약_항목은_근거_하나이고_위치는_time():
    items = summary_items([summ("사주 이야기", "사주", 91.92, 117.6), summ("  "), summ("구간 없음"),
                           {"text": "여러 구간", "evidence": [{"start": 10, "end": 20}, {"start": 3600.4, "end": 3725}]}])
    assert texts(items) == ["사주 이야기", "구간 없음", "여러 구간"]                     # 빈 글은 뺌
    assert items[0].location == {"type": "time", "startMs": 91920, "endMs": 117600, "label": "1:31"}
    assert items[0].tags == (TAG_SUMMARY, "주제 사주")
    assert items[1].location == {"type": "time", "startMs": None, "endMs": None, "label": "실시간 요약"}
    assert items[1].tags == (TAG_SUMMARY,)
    assert items[2].location["startMs"] == 10000 and items[2].location["endMs"] == 3725000


def test_가까운_순서를_매길_글은_주제와_앞부분():
    a, b = summary_items([summ("가" * 500, "사주"), summ("나")])
    assert summary_embed_text(a) == ("사주: " + "가" * 500)[:300] and summary_embed_text(b) == "나"


class FakeEmbedder:
    """글에 든 낱말로 벡터를 만든다: 질문의 낱말이 든 글이 가깝다. 부른 횟수를 센다."""
    WORDS = ("사주", "커피", "워크숍")

    def __init__(self):
        self.calls = 0

    def _vec(self, text):
        v = [1.0 if w in text else 0.0 for w in self.WORDS] + [0.01]
        n = sum(x * x for x in v) ** 0.5
        return [x / n for x in v]

    def embed_query(self, text):
        self.calls += 1
        return self._vec(text)

    def embed_passages(self, texts):
        self.calls += 1
        return [self._vec(t) for t in texts]


def test_요약이_상한_안이면_모두이고_임베딩을_부르지_않음():
    items = summary_items([summ("사주 " + "가" * 100), summ("커피 " + "나" * 100)])
    e = FakeEmbedder()
    assert pick_summary(items, "사주", e, max_chars=10_000) == items and e.calls == 0


def test_요약이_넘치면_질문과_가까운_것부터_고르고_시간_순서로_넘김():
    items = summary_items([summ(f"{w} " + "가" * 97, w) for w in ("사주", "커피", "워크숍", "커피", "사주")])   # 100자씩
    picked = pick_summary(items, "사주 이야기 뭐였지", FakeEmbedder(), max_chars=250)
    assert [p.order for p in picked] == [(0,), (4,)]                         # 처음과 끝의 사주 항목, 시간 순서
    picked = pick_summary(items, "커피 머신", FakeEmbedder(), max_chars=250)
    assert [p.order for p in picked] == [(1,), (3,)]


def test_요약_임베딩이_없으면_가장_최근_것부터():
    items = summary_items([summ(f"{i}" + "가" * 99) for i in range(5)])
    assert [p.order for p in pick_summary(items, "질문", None, max_chars=250)] == [(3,), (4,)]
    assert [p.order for p in pick_summary(items, "  ", FakeEmbedder(), max_chars=250)] == [(3,), (4,)]
