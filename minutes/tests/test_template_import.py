"""회의록 양식 가져오기 시험 (SCRUM-289). AI 는 정해 둔 답을 내는 가짜를 쓴다. 모델을 부르지 않는다.

PDF 읽기는 rag 시험 파일(upload_report.pdf: 4쪽, 쪽마다 같은 머리글과 "- 쪽 -" 바닥글, 번호 제목 "1. 개요" 등)을 빌려 쓴다.
양식 모양의 PDF 는 아직 없다 (실제 양식 샘플을 받은 뒤 추가).
"""
import json
from pathlib import Path

import pytest

from minutes import template_import as ti
from minutes.placement import CHECK_FAILED, LLM_FAILED, LLM_TIMEOUT, load_prompt
from minutes_fakes import ScriptLLM
from rag.errors import ENCRYPTED, NO_TEXT, TOO_LARGE, UNSUPPORTED_FORMAT
from shared.llm import LLMError, LLMTimeout

FIXTURES = Path(__file__).resolve().parents[2] / "rag" / "tests" / "fixtures"


def pdf(name):
    return (FIXTURES / name).read_bytes()


# 가짜 양식: 1쪽에 머리글, 항목 4개(체크박스 있는 칸, 표 포함), 2쪽 바닥글
PAGES = [
    (1, ["나인아웃팅 회의록", "회의명:", "1. 논의 내용", "※ 안건별로 적는다", "2. 결정 사항", "3. 확인 사항", "☐ 자료 공유", "☐ 일정 확인",
         "4. 할 일", "항목 담당 기한"]),
    (2, ["특이사항", "- 2 -"]),
]


@pytest.fixture
def lines():
    return ti.number_lines(PAGES)


def ref_of(lines, text):
    return next(x.ref for x in lines if x.text == text)


def good(lines):
    r = lambda t: ref_of(lines, t)      # noqa: E731
    return {
        "header": [r("나인아웃팅 회의록")],
        "footer": [r("- 2 -")],
        "fields": [
            {"ref": r("회의명:"), "title": "회의명", "type": "free", "columns": None, "descriptionRefs": []},
            {"ref": r("1. 논의 내용"), "title": "논의 내용", "type": "free", "columns": None, "descriptionRefs": [r("※ 안건별로 적는다")]},
            {"ref": r("2. 결정 사항"), "title": "결정 사항", "type": "list", "columns": None, "descriptionRefs": []},
            {"ref": r("3. 확인 사항"), "title": "확인 사항", "type": "checklist", "columns": None, "descriptionRefs": []},
            {"ref": r("4. 할 일"), "title": "할 일", "type": "table", "columns": 3, "descriptionRefs": []},
        ],
    }


# ---- 입력 만들기

def test_번호는_문서_전체에서_이어서_센다(lines):
    assert [x.ref for x in lines] == list(range(1, 13))
    assert (lines[0].page, lines[0].pos, lines[0].page_len) == (1, 0, 10)
    assert (lines[10].page, lines[10].pos, lines[10].page_len) == (2, 0, 2)


def test_AI_입력에_줄_번호와_쪽이_붙고_꺾쇠는_바뀐다():
    ls = ti.number_lines([(1, ["a <b> c"])])
    text = ti.build_user(ls, ["문제 하나"])
    assert "L1 (1쪽) a ‹b› c" in text
    assert text.index("<양식 글>") < text.index("</양식 글>") < text.index("<지난 답의 문제>")
    assert "- 문제 하나" in text and "<지난 답의 문제>" not in ti.build_user(ls)


def test_글이_없으면_no_text_너무_크면_too_large(monkeypatch):
    with pytest.raises(ti.IngestError) as e:
        ti.number_lines([])
    assert e.value.code == NO_TEXT
    monkeypatch.setattr(ti, "MAX_CHARS", 5)
    with pytest.raises(ti.IngestError) as e:
        ti.number_lines([(1, ["여섯 글자입니다"])])
    assert e.value.code == TOO_LARGE


# ---- 코드 검사

def test_맞는_답은_양식_항목이_된다(lines):
    got, problems = ti.check(good(lines), lines)
    assert problems == []
    assert [f["title"] for f in got.fields] == ["회의명", "논의 내용", "결정 사항", "확인 사항", "할 일"]
    assert [f["type"] for f in got.fields] == ["free", "free", "list", "checklist", "table"]
    assert got.fields[4]["columns"] == 3 and "columns" not in got.fields[0]
    assert got.fields[1]["description"] == "※ 안건별로 적는다" and "description" not in got.fields[0]
    assert all(f["aiFill"] is True for f in got.fields)
    assert got.header == "나인아웃팅 회의록" and got.footer == "- 2 -" and got.warnings == []


def test_제목은_줄에_실제로_있어야_하고_번호와_띄어쓰기는_달라도_된다(lines):
    data = good(lines)
    data["fields"][1]["title"] = "논의내용"                       # 띄어쓰기 차이는 허용
    assert ti.check(data, lines)[0] is not None
    data["fields"][1]["title"] = "회의 안건"                       # 줄에 없음
    got, problems = ti.check(data, lines)
    assert got is None and any("글에 없거나" in p for p in problems)


@pytest.mark.parametrize("mutate, expect", [
    (lambda d, r: d["fields"][0].update(ref=999), "없는 줄 번호"),
    (lambda d, r: d["fields"][2].update(type="paragraph"), "type"),
    (lambda d, r: d["fields"][4].update(columns=None), "columns"),
    (lambda d, r: d["fields"][4].update(columns=13), "columns"),
    (lambda d, r: d["fields"][4].update(columns=True), "columns"),
    (lambda d, r: d["fields"].reverse(), "순서"),
    (lambda d, r: d["fields"][1].update(ref=d["fields"][0]["ref"], title="회의명"), "title이"),      # 같은 줄이어도 제목이 같으면 안 됨
    (lambda d, r: d.update(header=[r("특이사항")]), "header"),         # 2쪽 줄은 머리글이 될 수 없다
    (lambda d, r: d.update(header=[r("1. 논의 내용")]), "머리글·바닥글로 쓴 줄"),   # 항목 제목 줄을 머리글로도 쓸 수 없다
    (lambda d, r: d.update(footer=[r("1. 논의 내용")]), "footer"),
    (lambda d, r: d.update(header=[999]), "header"),
    (lambda d, r: d.update(header="나인아웃팅 회의록"), "header"),
    (lambda d, r: d["fields"][2].update(descriptionRefs=[d["fields"][3]["ref"]]), "겹침"),
    (lambda d, r: d["fields"][2].update(title="  ", ref=d["fields"][2]["ref"]), "title"),
])
def test_틀린_답은_이유와_함께_걸린다(lines, mutate, expect):
    data = good(lines)
    mutate(data, lambda t: ref_of(lines, t))
    got, problems = ti.check(data, lines)
    assert got is None and any(expect in p for p in problems), problems


def test_제목이_겹치거나_20개를_넘거나_JSON이_아니면_걸린다(lines):
    data = good(lines)
    data["fields"][2]["title"] = "결 정 사 항"
    data["fields"][3].update(title="결정 사항", ref=data["fields"][3]["ref"])
    assert ti.check(data, lines)[0] is None            # 3번 줄에 "결정 사항"이 없어 걸린다 (제목이 줄에 없음)
    many = {"header": [], "footer": [], "fields": [
        {"ref": 1, "title": "나인아웃팅", "type": "free", "columns": None, "descriptionRefs": []} for _ in range(21)]}
    assert any("최대 20개" in p for p in ti.check(many, lines)[1])
    assert ti.check(None, lines)[0] is None and ti.check({"fields": "x"}, lines)[0] is None


def test_한_줄에_칸_이름이_여럿이면_같은_줄_번호를_줄에_나온_순서대로_쓴다():
    ls = ti.number_lines([(1, ["주간 회의록", "회의명 일시", "장소 참석자", "1. 논의 내용"])])
    r = lambda t: next(x.ref for x in ls if x.text == t)      # noqa: E731
    field = lambda ref, title: {"ref": ref, "title": title, "type": "free", "columns": None, "descriptionRefs": []}      # noqa: E731
    two = {"header": [], "footer": [], "fields": [field(r("회의명 일시"), "회의명"), field(r("회의명 일시"), "일시"),
                                                   field(r("장소 참석자"), "장소"), field(r("장소 참석자"), "참석자"),
                                                   field(r("1. 논의 내용"), "논의 내용")]}
    got, problems = ti.check(two, ls)
    assert problems == [] and [f["title"] for f in got.fields] == ["회의명", "일시", "장소", "참석자", "논의 내용"]
    two["fields"][0], two["fields"][1] = two["fields"][1], two["fields"][0]        # 줄에 나온 순서와 다름
    got, problems = ti.check(two, ls)
    assert got is None and any("앞 항목보다 앞" in p for p in problems), problems


def test_다른_쪽의_같은_머리글_바닥글을_골라도_첫_쪽_마지막_쪽_것으로_본다():
    ls = ti.number_lines([(1, ["Doc 1", "1. 개요", "- 1 -"]), (2, ["Doc 1", "2. 내용", "- 2 -"])])
    r = lambda t, nth=0: [x.ref for x in ls if x.text == t][nth]      # noqa: E731
    field = {"ref": r("1. 개요"), "title": "개요", "type": "free", "columns": None, "descriptionRefs": []}
    got, problems = ti.check({"header": [r("Doc 1", 1)], "footer": [r("- 1 -")], "fields": [field]}, ls)
    assert problems == [] and got.header == "Doc 1" and got.footer == "- 2 -"       # 2쪽 머리글 → 1쪽 것, 1쪽 바닥글 → 마지막 쪽 것
    got, problems = ti.check({"header": [r("2. 내용")], "footer": [], "fields": [field]}, ls)
    assert got is None and any("header" in p for p in problems)                       # 같은 글이 아니면 그대로 걸린다


def test_줄_번호를_문자열_L3나_3으로_써도_읽고_이상한_글은_거른다(lines):
    data = good(lines)
    for f in data["fields"]:
        f["ref"] = f"L{f['ref']}"
    data["header"], data["footer"] = [f"L{data['header'][0]}"], [str(data["footer"][0])]
    data["fields"][1]["descriptionRefs"] = [f"L{data['fields'][1]['descriptionRefs'][0]}"]
    got, problems = ti.check(data, lines)
    assert problems == [] and [f["title"] for f in got.fields][1] == "논의 내용" and got.header == "나인아웃팅 회의록"
    data["fields"][0]["ref"] = "행3"
    assert ti.check(data, lines)[0] is None
    data["fields"][0]["ref"] = True
    assert ti.check(data, lines)[0] is None


def test_columns와_descriptionRefs를_생략해도_된다(lines):
    data = good(lines)
    for f in data["fields"]:
        f.pop("descriptionRefs"), (f.pop("columns") if f["type"] != "table" else None)
    got, problems = ti.check({k: v for k, v in data.items() if k == "fields"}, lines)
    assert problems == [] and got.header is None and got.footer is None and got.fields[4]["columns"] == 3


def test_체크박스가_없는_체크리스트는_종류를_두고_경고만_한다(lines):
    data = good(lines)
    data["fields"][2]["type"] = "checklist"             # "2. 결정 사항" 안에 체크박스 글자가 없음
    got, problems = ti.check(data, lines)
    assert problems == [] and got.fields[2]["type"] == "checklist" and got.fields[3]["type"] == "checklist"
    assert got.warnings == [{"code": ti.CHECKLIST_NO_BOX, "title": "결정 사항"}]       # 체크박스가 있는 칸은 경고 없음


def test_열이_10보다_많은_표는_10으로_자르고_센_값을_경고로_남긴다(lines):
    data = good(lines)
    data["fields"][4]["columns"] = 12
    got, problems = ti.check(data, lines)
    assert problems == [] and got.fields[4]["columns"] == 10
    assert got.warnings == [{"code": ti.COLUMNS_CAPPED, "title": "할 일", "detected": 12}]
    data["fields"][4]["columns"] = 10
    assert ti.check(data, lines)[0].warnings == []


def test_빈칸이_든_서식_문장은_설명에서_빼고_경고한다():
    ls = ti.number_lines([(1, ["1. 개요", "의장은 ____ 에 개회를 선언하였다.", "2. 결정", "※ 결정된 것만 적는다"])])
    r = lambda t: next(x.ref for x in ls if x.text == t)      # noqa: E731
    field = lambda t, d: {"ref": r(t), "title": t[3:], "type": "free", "descriptionRefs": [r(d)]}      # noqa: E731
    got, problems = ti.check({"fields": [field("1. 개요", "의장은 ____ 에 개회를 선언하였다."), field("2. 결정", "※ 결정된 것만 적는다")]}, ls)
    assert problems == [] and "description" not in got.fields[0] and got.fields[1]["description"] == "※ 결정된 것만 적는다"
    assert got.warnings == [{"code": ti.DESCRIPTION_DROPPED, "title": "개요"}]


def test_항목이_없는_답은_통과하고_머리글_바닥글은_없을_수_있다(lines):
    got, problems = ti.check({"header": [], "footer": [], "fields": []}, lines)
    assert problems == [] and got.fields == [] and got.header is None and got.footer is None


# ---- 처음부터 끝까지 (PDF → 결과)

def report_answer(lines):
    r = lambda t: ref_of(lines, t)      # noqa: E731
    return {"header": [r("나인아웃팅 합성 자료 · 내부 검토용")], "footer": [],
            "fields": [{"ref": r("1. 개요"), "title": "개요", "type": "free", "columns": None, "descriptionRefs": []}]}


def test_PDF에서_양식을_읽어_돌려준다():
    lines = ti.read_lines(pdf("upload_report.pdf"))
    assert {x.page for x in lines} == {1, 2, 4}                          # 3쪽은 빈 쪽
    llm = ScriptLLM(report_answer(lines))
    out = ti.import_template(pdf("upload_report.pdf"), llm, 30)
    assert out.error_code is None and out.attempts == 1 and (out.input_tokens, out.output_tokens) == (100, 20)
    assert out.result.fields == [{"title": "개요", "type": "free", "aiFill": True}]
    assert out.result.header == "나인아웃팅 합성 자료 · 내부 검토용" and out.result.footer is None
    system, user, _ = llm.calls[0]
    assert system == load_prompt(None, ti.KIND).text and "L1 (1쪽) 나인아웃팅 합성 자료" in user


def test_검사에_걸리면_이유를_붙여_다시_시킨다():
    lines = ti.read_lines(pdf("upload_report.pdf"))
    bad = report_answer(lines)
    bad["fields"][0]["title"] = "없는 제목"
    llm = ScriptLLM(bad, report_answer(lines))
    out = ti.import_template(pdf("upload_report.pdf"), llm, 30)
    assert out.error_code is None and out.attempts == 2
    assert "<지난 답의 문제>" not in llm.calls[0][1] and "title이" in llm.calls[1][1]


def test_계속_틀리면_CHECK_FAILED_호출이_계속_실패하면_LLM_오류():
    lines = ti.read_lines(pdf("upload_report.pdf"))
    bad = report_answer(lines)
    bad["fields"][0]["ref"] = 999
    out = ti.import_template(pdf("upload_report.pdf"), ScriptLLM(bad, bad, bad), 30)
    assert (out.result, out.error_code, out.attempts) == (None, CHECK_FAILED, 3)
    out = ti.import_template(pdf("upload_report.pdf"), ScriptLLM(LLMTimeout("t"), LLMTimeout("t")), 30)
    assert (out.result, out.error_code) == (None, LLM_TIMEOUT)
    out = ti.import_template(pdf("upload_report.pdf"), ScriptLLM(LLMError("x"), LLMError("x")), 30)
    assert out.error_code == LLM_FAILED


def test_항목을_찾지_못하면_NO_FIELDS():
    out = ti.import_template(pdf("upload_report.pdf"), ScriptLLM({"header": [], "footer": [], "fields": []}), 30)
    assert (out.result, out.error_code) == (None, ti.NO_FIELDS) and out.attempts == 1


def test_글_안의_JSON_울타리와_앞뒤_말은_견딘다():
    lines = ti.read_lines(pdf("upload_report.pdf"))
    wrapped = "답입니다.\n```json\n" + json.dumps(report_answer(lines), ensure_ascii=False) + "\n```"
    assert ti.import_template(pdf("upload_report.pdf"), ScriptLLM(wrapped), 30).error_code is None


@pytest.mark.parametrize("name, code", [("upload_scan.pdf", NO_TEXT), ("upload_locked.pdf", ENCRYPTED)])
def test_읽을_수_없는_PDF는_AI를_부르지_않고_코드로_알린다(name, code):
    llm = ScriptLLM()
    out = ti.import_template(pdf(name), llm, 30)
    assert (out.result, out.error_code, out.attempts) == (None, code, 0) and llm.calls == []


def _zip_with(*names):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, "<x/>")
    return buf.getvalue()


@pytest.mark.parametrize("label, data", [
    ("png 이미지", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
    ("옛 doc·hwp(OLE)", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64),
    ("hwpx(zip)", _zip_with("[Content_Types].xml", "Contents/section0.xml")),
    ("pptx(zip)", _zip_with("[Content_Types].xml", "ppt/presentation.xml")),
])
def test_지원하지_않는_형식은_AI를_부르지_않고_unsupported_format(label, data):
    llm = ScriptLLM()
    out = ti.import_template(data, llm, 30)
    assert (out.result, out.error_code, out.attempts) == (None, UNSUPPORTED_FORMAT, 0) and llm.calls == [], label


def test_쪽이_너무_많으면_too_large(monkeypatch):
    monkeypatch.setattr(ti, "MAX_PAGES", 2)
    assert ti.import_template(pdf("upload_report.pdf"), ScriptLLM(), 30).error_code == TOO_LARGE


def test_지시문_파일이_읽힌다():
    p = load_prompt(None, ti.KIND)
    assert p.version == "template_import-v1" and "descriptionRefs" in p.text


# ---- PDF 말고 워드·엑셀·글 파일 (형식은 파일 내용으로 판단)

def make_docx():
    import io
    from docx import Document
    d = Document()
    d.sections[0].header.paragraphs[0].text = "나인아웃팅 | 내부 문서"
    d.sections[0].footer.paragraphs[0].text = "- 1 -"
    d.add_heading("주간 업무 회의록", 1)
    d.add_paragraph("")                                           # 빈 문단은 뺀다
    info = d.add_table(rows=2, cols=4)
    for cell, text in zip(info.rows[0].cells, ["회의명", "", "일시", ""]):
        cell.text = text
    for cell, text in zip(info.rows[1].cells, ["", "", "", ""]):   # 모두 빈 행은 뺀다
        cell.text = text
    d.add_paragraph("1. 논의 내용")
    d.add_paragraph("※ 안건별로 적는다")
    d.add_paragraph("2. 할 일")
    t = d.add_table(rows=3, cols=3)
    for cell, text in zip(t.rows[0].cells, ["할 일", "담당자", "기한"]):
        cell.text = text
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_xlsx(hidden=False):
    import io
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "양식"
    ws.append(["회의록"])
    ws.append(["회의명", None, "일시", None])
    ws.append([None, None, None, None])                           # 빈 행은 뺀다
    ws.append(["1. 논의 내용"])
    ws.append(["할 일", "담당자", "기한"])
    if hidden:
        memo = wb.create_sheet("메모")
        memo.append(["숨긴 시트 글"])
        memo.sheet_state = "hidden"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_워드는_머리글_본문_바닥글_순서로_읽고_표는_칸을_잇는다():
    pytest.importorskip("docx")
    texts = [x.text for x in ti.read_lines(make_docx())]
    assert texts == ["나인아웃팅 | 내부 문서", "주간 업무 회의록", "회의명 | 일시", "1. 논의 내용", "※ 안건별로 적는다", "2. 할 일",
                     "할 일 | 담당자 | 기한", "- 1 -"], texts


def test_워드의_세로_병합_칸은_한_번만_읽는다():
    pytest.importorskip("docx")
    import io
    from docx import Document
    d = Document()
    t = d.add_table(rows=2, cols=3)
    t.cell(0, 0).merge(t.cell(1, 0)).text = "결 재"                    # 결재란처럼 두 줄에 걸친 칸
    for j, c in enumerate(["담당", "팀장"]):
        t.cell(0, j + 1).text = c
    buf = io.BytesIO()
    d.save(buf)
    assert [x.text for x in ti.read_lines(buf.getvalue())] == ["결 재 | 담당 | 팀장"]


def test_엑셀은_보이는_시트의_글이_있는_행만_읽는다():
    pytest.importorskip("openpyxl")
    texts = [x.text for x in ti.read_lines(make_xlsx(hidden=True))]
    assert texts == ["회의록", "회의명 | 일시", "1. 논의 내용", "할 일 | 담당자 | 기한"], texts       # 숨긴 시트와 빈 행은 없음
    import io
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.title = "A"
    wb.active.append(["가"])
    wb.create_sheet("B").append(["나"])
    buf = io.BytesIO()
    wb.save(buf)
    assert [x.text for x in ti.read_lines(buf.getvalue())] == ["A", "가", "B", "나"], "시트가 둘 이상이면 이름 줄이 앞에 들어간다"


@pytest.mark.parametrize("label, data, expected", [
    ("utf-8", "회의록\n\n1. 논의 내용\n할 일 | 담당자".encode("utf-8"), ["회의록", "1. 논의 내용", "할 일 | 담당자"]),
    ("utf-8 BOM", "﻿회의록\r\n결정 사항".encode("utf-8"), ["회의록", "결정 사항"]),
    ("cp949", "회의록\n논의 내용".encode("cp949"), ["회의록", "논의 내용"]),
    ("마크다운 표", "# 회의록\n\n## 논의 내용\n| 할 일 | 담당자 | 기한 |\n|---|:---:|---|\n| 1 | | |\n|  |  |  |\n| a|b | c |".encode("utf-8"),
     ["# 회의록", "## 논의 내용", "할 일 | 담당자 | 기한", "1", "a | b | c"]),
    ("글 안의 파이프는 그대로", "A | B\n|시작만 파이프".encode("utf-8"), ["A | B", "|시작만 파이프"]),
    ("csv", "항목,담당자,기한\n할 일,,".encode("utf-8"), ["항목,담당자,기한", "할 일,,"]),
])
def test_글_파일은_줄마다_읽는다(label, data, expected):
    assert [x.text for x in ti.read_lines(data)] == expected, label


def test_글이_없는_워드_엑셀_글_파일은_no_text():
    pytest.importorskip("docx")
    import io
    from docx import Document
    buf = io.BytesIO()
    Document().save(buf)
    for data in (buf.getvalue(), b"  \n\n \n", b""):
        out = ti.import_template(data, ScriptLLM(), 30)
        assert (out.result, out.error_code) == (None, NO_TEXT)


def test_워드에서도_항목과_머리글_바닥글을_읽어_돌려준다():
    pytest.importorskip("docx")
    data = make_docx()
    lines = ti.read_lines(data)
    r = lambda text: next(x.ref for x in lines if x.text == text)      # noqa: E731
    llm = ScriptLLM({"header": [r("나인아웃팅 | 내부 문서")], "footer": [r("- 1 -")], "fields": [
        {"ref": r("1. 논의 내용"), "title": "논의 내용", "type": "free", "descriptionRefs": [r("※ 안건별로 적는다")]},
        {"ref": r("2. 할 일"), "title": "할 일", "type": "table", "columns": 3}]})
    out = ti.import_template(data, llm, 30)
    assert out.error_code is None and out.attempts == 1
    assert out.result.header == "나인아웃팅 | 내부 문서" and out.result.footer == "- 1 -"
    assert [(f["title"], f["type"], f.get("columns")) for f in out.result.fields] == [("논의 내용", "free", None), ("할 일", "table", 3)]
    assert out.result.fields[0]["description"] == "※ 안건별로 적는다"
    assert "L1 (1쪽) 나인아웃팅 | 내부 문서" in llm.calls[0][1]


# ---- 받는 형식 제한 (formats)

def test_받는_형식을_정하면_그_밖의_형식은_AI를_부르기_전에_거절한다():
    import io
    from openpyxl import Workbook
    wb = Workbook()
    wb.active["A1"] = "1. 개요"
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    assert ti.read_lines(data)                                              # 정하지 않으면 읽는다
    with pytest.raises(ti.IngestError) as e:
        ti.read_lines(data, ti.PRODUCT_FORMATS)
    assert e.value.code == UNSUPPORTED_FORMAT
    llm = ScriptLLM()
    out = ti.import_template(data, llm, 30, formats=ti.PRODUCT_FORMATS)
    assert out.error_code == UNSUPPORTED_FORMAT and out.attempts == 0 and llm.calls == []
    assert ti.read_lines(pdf("upload_report.pdf"), ti.PRODUCT_FORMATS)       # PDF 는 통과
    assert ti.PRODUCT_FORMATS == frozenset({"pdf", "docx"}) and ti.ALL_FORMATS == frozenset({"pdf", "docx", "xlsx", "text"})
