"""워드 읽기 시험 (SCRUM-108). 합성 샘플은 fixtures/make_upload_samples.py 로 만든다."""
import io
from pathlib import Path

import pytest

from rag.errors import NO_TEXT, UNSUPPORTED_FORMAT, IngestError
from rag.filetype import DOCX, detect
from rag.loaders.docx import load_docx

FIX = Path(__file__).parent / "fixtures"
EDGE = (FIX / "upload_edge.docx").read_bytes()
PLAN = (FIX / "upload_plan.docx").read_bytes()


def code_of(data):
    with pytest.raises(IngestError) as e:
        load_docx(data)
    return e.value.code


def test_형식_확인에서_워드로_알아낸다():
    assert detect(EDGE) == DOCX and detect(PLAN) == DOCX


def test_제목마다_조각_하나_위치는_가장_가까운_제목_맥락은_제목_경로():
    start, rules, times, tidy = load_docx(EDGE).pieces
    assert start.location == {"type": "section", "heading": "", "label": "문서 처음"} and start.context is None
    assert start.content == "표지: 워드 읽기 경계 시험 문서 (합성 자료)"
    assert rules.location == {"type": "section", "heading": "1. 회의실 예약 규칙", "label": "1. 회의실 예약 규칙"}
    assert times.context == "1. 회의실 예약 규칙 > 1.1 예약 시간"
    assert tidy.context == "2. 정리 규칙"                                    # 같은 수준의 제목이 오면 경로가 바뀐다
    assert {p.kind for p in (start, rules, times, tidy)} == {"section"}


def test_표는_행마다_머리_행_이름과_함께_병합된_칸은_행마다_붙는다():
    times = load_docx(EDGE).pieces[2]
    assert times.content.split("\n")[2:] == [
        "구분: 평일 | 시간: 09:00~12:00 | 비고: 오전",
        "구분: 평일 | 시간: 13:00~18:00 | 비고: 오후",      # 세로로 병합된 "평일"
        "구분: 주말 | 시간: 예약 불가",                       # 빈 칸은 빼고, 빈 행은 건너뜀
    ]


def test_머리글_바닥글은_읽지_않고_한글과_보이지_않는_문자는_정리한다():
    pieces = load_docx(EDGE).pieces
    text = "\n".join(p.content for p in pieces)
    assert "나인아웃팅 합성 자료" not in text and "고객성공팀" not in text
    assert pieces[3].content.split("\n")[1] == "회의가 끝나면 화이트보드를 지운다."   # 자모로 쪼갠 한글 + 폭 없는 공백


def test_제목_한_줄뿐인_조각은_넣지_않는다():
    """운영계획서의 "1. 행사 개요" 바로 아래에 "1.1 목적" 이 온다"""
    r = load_docx(PLAN)
    labels = [p.location["label"] for p in r.pieces]
    assert "1. 행사 개요" not in labels and "1.1 목적" in labels
    assert next(p for p in r.pieces if p.location["label"] == "1.1 목적").context.endswith("1. 행사 개요 > 1.1 목적")


def test_운영계획서_표와_목록을_꺼낸다():
    text = "\n".join(p.content for p in load_docx(PLAN).pieces)
    lines = text.split("\n")
    assert "항목: 케이터링 | 금액: 6,750,000원 | 산출 근거: 150명 × 2일, 1인 1식 22,500원" in lines
    assert "역할: 안전 책임자 | 인원: 1 | 주요 업무: 안전 점검, 비상 대응 지휘, 인원 집계 | 근무 시간: 양일 집결부터 철수까지" in lines
    assert any(line.startswith("식이 제한: 사전 조사 결과 채식 12명") for line in lines)


def test_본문에_글이_없으면_no_text():
    assert code_of((FIX / "upload_empty.docx").read_bytes()) == NO_TEXT


def test_열_수_없는_워드는_unsupported_format():
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:                  # 형식 확인은 통과하지만 내용이 깨진 워드
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<깨진")
    assert detect(buf.getvalue()) == DOCX
    assert code_of(buf.getvalue()) == UNSUPPORTED_FORMAT
