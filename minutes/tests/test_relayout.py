"""다시 배치의 재료 만들기 시험 (명세 8장). 합성 요청으로 처음 초안을 만들고, 그 줄을 고쳐 previousLines 로 쓴다."""
from minutes.draft import make_draft
from minutes.relayout import WRITTEN, build_relayout, flatten
from minutes_fakes import GOOD, ScriptLLM, load_request, previous_lines_of


def first_draft():
    """처음 초안: 9줄 (m9 의견은 넣지 못함). 줄 순서는 칸 순서 → 칸 안 재료 순서."""
    body = load_request()
    return body, previous_lines_of(make_draft(body, ScriptLLM(GOOD)))


def relayout(body, lines):
    return build_relayout(body["agendas"], body["summary"], lines)


def find(lines, kind, item_id=None):
    return next(i for i, l in enumerate(lines) if l["sources"] and l["sources"][0]["kind"] == kind
                and l["sources"][0]["id"] == item_id)


def test_아무것도_고치지_않으면_처음_재료를_그대로_다시_만든다():
    body, lines = first_draft()
    materials, unreferenced = relayout(body, lines)
    assert [m.ref for m in materials] == [f"m{i}" for i in range(1, 10)]
    assert [m.previous for m in materials] == list(range(9))
    assert [m.order for m in materials] == [(i, 0, 0) for i in range(9)]       # 이전 초안의 줄 순서
    assert [m.sources() for m in materials] == [l["sources"] for l in lines]
    assert not any(m.whole_line or m.draft_edited for m in materials)
    decision = materials[find(lines, "decision")]
    assert decision.rewrite                                                    # 결정은 AI 가 다시 다듬는다
    assert [(m.kind, m.item_id) for m in unreferenced] == [("opinion", "k3f9a1-20")]   # 처음에 넣지 못한 m9


def test_작성자가_고친_줄은_글과_근거를_그대로_두고_칸만_다시_고른다():
    body, lines = first_draft()
    i = find(lines, "decision")
    lines[i].update(sentence="배포 일정: 1차 배포를 10월 30일(목) 오후로 정했다.", draftEdited=True, transcriptSeqs=[40])
    m = relayout(body, lines)[0][i]
    assert (m.fixed_text, m.text) == ("배포 일정: 1차 배포를 10월 30일(목) 오후로 정했다.",) * 2
    assert m.whole_line and m.draft_edited and not m.rewrite
    assert m.evidence_seqs == (40,) and m.sources() == lines[i]["sources"]
    assert (m.label, m.agenda_title) == ("결정", "배포 일정")                  # AI 입력에 보여 줄 값은 원래 재료에서


def test_작성자가_새로_쓴_줄은_출처_없이_글_그대로():
    body, lines = first_draft()
    lines.insert(2, {"title": "할 일", "sentence": "고객사 로고는 행사 전날까지 받기", "transcriptSeqs": [],
                     "edited": False, "needsCheck": False, "sources": [], "draftEdited": True})
    materials, _ = relayout(body, lines)
    m = materials[2]
    assert (m.kind, m.agenda_id, m.label, m.sources()) == (WRITTEN, None, "직접 작성", [])
    assert m.fixed_text == "고객사 로고는 행사 전날까지 받기" and m.whole_line and m.order == (2, 0, 0)
    assert len(materials) == 10


def test_출처_항목이_요약에_없으면_고친_줄처럼_글을_지킨다():
    body, lines = first_draft()
    i = find(lines, "todo", "k3f9a1-10")
    body["summary"]["agendas"][0]["todos"] = []                               # 초안 뒤 요약 점검에서 지운 할 일
    m = relayout(body, lines)[0][i]
    assert m.fixed_text == lines[i]["sentence"] and m.whole_line
    assert not m.draft_edited                                                  # 들어온 값 그대로
    assert m.sources() == [{"agendaId": 101, "kind": "todo", "id": "k3f9a1-10"}] and m.label == "할 일"


def test_복사한_줄은_첫_줄만_출처대로_나머지는_글을_지킨다():
    body, lines = first_draft()
    i = find(lines, "decision")
    copy = dict(lines[i], title="일정", sentence="배포 일정: 1차 배포 10/30 (QA 끝난 뒤)", draftEdited=True)
    lines.insert(0, copy)                                                      # 고친 복사본이 앞에 와도
    materials, unreferenced = relayout(body, lines)
    assert materials[0].whole_line and materials[0].fixed_text == copy["sentence"]
    assert materials[i + 1].rewrite and not materials[i + 1].whole_line     # 고치지 않은 줄이 출처대로
    assert materials[0].sources() == materials[i + 1].sources()            # 출처·근거는 둘 다 유지
    assert len({m.ref for m in materials}) == 10                             # 재료 번호는 따로
    assert len(unreferenced) == 1


def test_고치지_않은_복사본이_둘이면_앞의_줄만_출처대로():
    body, lines = first_draft()
    i = find(lines, "decision")
    lines.append(dict(lines[i]))
    materials, _ = relayout(body, lines)
    assert materials[i].rewrite and materials[-1].whole_line and not materials[-1].draft_edited


def test_작성자가_지운_줄의_요약_항목은_다시_넣지_않는다():
    body, lines = first_draft()
    del lines[find(lines, "todo", "u-1")]
    materials, unreferenced = relayout(body, lines)
    assert len(materials) == 8
    assert {(m.kind, m.item_id) for m in unreferenced} == {("opinion", "k3f9a1-20"), ("todo", "u-1")}


def test_고정한_글의_줄바꿈은_공백으로():
    assert flatten("  첫 줄\n  둘째 줄 \r\n셋째\n") == "첫 줄 둘째 줄 셋째"
    body, lines = first_draft()
    lines[0].update(sentence="배포 일정:\n오후로 변경", draftEdited=True)
    assert relayout(body, lines)[0][0].fixed_text == "배포 일정: 오후로 변경"


def test_출처가_둘이면_첫_출처만_쓴다():
    body, lines = first_draft()
    i = find(lines, "summary", "k3f9a1-1")
    lines[i]["sources"].append({"agendaId": 102, "kind": "summary", "id": "k3f9a1-3"})
    materials, unreferenced = relayout(body, lines)
    assert materials[i].sources() == [{"agendaId": 101, "kind": "summary", "id": "k3f9a1-1"}]
    assert materials[i].rewrite is False and not materials[i].whole_line   # 출처대로 다시 만든 고정 재료
