"""엑셀 읽기 시험 (SCRUM-109). 합성 샘플은 fixtures/make_upload_samples.py 로 만든다."""
import io
from pathlib import Path

import pytest

from rag.errors import NO_TEXT, TOO_LARGE, UNSUPPORTED_FORMAT, IngestError
from rag.filetype import XLSX, detect
from rag.loaders import xlsx as xlsx_loader
from rag.loaders.xlsx import even_groups, load_xlsx

FIX = Path(__file__).parent / "fixtures"
EDGE = (FIX / "upload_edge.xlsx").read_bytes()
PLAN = (FIX / "upload_plan.xlsx").read_bytes()


def code_of(data):
    with pytest.raises(IngestError) as e:
        load_xlsx(data)
    return e.value.code


def test_형식_확인에서_엑셀로_알아낸다():
    assert detect(EDGE) == XLSX and detect(PLAN) == XLSX


def test_머리_행_이름과_값_병합_칸과_숫자_서식():
    budget = load_xlsx(EDGE).pieces[0]
    assert budget.location == {"type": "sheet", "sheet": "1분기 예산", "rows": [2, 6], "label": "1분기 예산 시트 2~6행"}
    assert budget.context == "1분기 예산" and budget.kind == "sheet"
    assert budget.content.split("\n") == [
        "항목: 온라인 광고 | 분류: 홍보 | 금액: 12,000,000 | 비율: 25% | 집행일: 2026-11-03 | 비고: 검색 광고 포함",
        "항목: 초청장 인쇄 | 분류: 홍보 | 금액: 800,000 | 비율: 5% | 집행일: 2026-10-21",   # 병합된 "홍보"
        "항목: 장소 대관 | 분류: 운영 | 금액: 9,000,000",                                       # 빈 칸은 뺌
        "항목: 합계 | 비고: 수식 칸",                    # 빈 행은 건너뛰고, 계산 결과가 없는 수식 칸은 빠짐
    ]


def test_행_번호는_엑셀_화면_그대로_긴_표는_고르게_나눈다():
    companies = [p for p in load_xlsx(EDGE).pieces if p.location["sheet"] == "참가 회사"]
    assert [p.location["rows"] for p in companies] == [[4, 11], [12, 19], [20, 26]]   # 머리 행은 3행, 23행을 8·8·7
    assert companies[0].content.split("\n")[0] == "회사: 합성회사01 | 담당 부서: 운영팀 | 참석 인원: 1"


def test_숨긴_시트와_빈_시트는_읽지_않는다():
    r = load_xlsx(EDGE)
    assert {p.location["sheet"] for p in r.pieces} == {"1분기 예산", "참가 회사"} and r.skipped == 2
    assert "숨긴 메모" not in "\n".join(p.content for p in r.pieces)


def test_운영계획서_표를_시트별로_꺼낸다():
    r = load_xlsx(PLAN)
    budget = next(p for p in r.pieces if p.location["sheet"] == "7. 예산")
    assert "항목: 케이터링 | 금액: 6,750,000원 | 산출 근거: 150명 × 2일, 1인 1식 22,500원" in budget.content.split("\n")
    schedule = [p.location["label"] for p in r.pieces if p.location["sheet"].startswith("2. 추진 일정")]
    assert schedule == ["2. 추진 일정 (준비 단계) 시트 2~7행", "2. 추진 일정 (준비 단계) 시트 8~12행"]   # 11행 → 6·5


def test_고르게_나누기():
    assert [len(g) for g in even_groups(list(range(11)), 10)] == [6, 5]
    assert [len(g) for g in even_groups(list(range(23)), 10)] == [8, 8, 7]
    assert [len(g) for g in even_groups(list(range(10)), 10)] == [10]


def test_보이는_시트에_글이_없으면_no_text():
    assert code_of((FIX / "upload_empty.xlsx").read_bytes()) == NO_TEXT


def test_칸이_상한을_넘으면_too_large(monkeypatch):
    monkeypatch.setattr(xlsx_loader, "MAX_CELLS", 10)
    assert code_of(EDGE) == TOO_LARGE


def test_열_수_없는_엑셀은_unsupported_format():
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:                  # 형식 확인은 통과하지만 내용이 깨진 엑셀
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<깨진")
    assert detect(buf.getvalue()) == XLSX
    assert code_of(buf.getvalue()) == UNSUPPORTED_FORMAT
