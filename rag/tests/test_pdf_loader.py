"""PDF 읽기 시험 (SCRUM-107). 합성 샘플은 fixtures/make_upload_samples.py 로 만든다."""
import re
import unicodedata
from pathlib import Path

import pytest

from rag.chunking import split_pieces
from rag.errors import ENCRYPTED, NO_TEXT, TOO_LARGE, UNSUPPORTED_FORMAT, IngestError
from rag.filetype import detect
from rag.loaders import pdf as pdf_loader
from rag.loaders.pdf import heading_level, load_pdf, strip_header_footer
from rag.loaders.text import normalize

FIX = Path(__file__).parent / "fixtures"
REPORT = (FIX / "upload_report.pdf").read_bytes()
PLAN = (FIX / "upload_plan.pdf").read_bytes()


def code_of(data):
    with pytest.raises(IngestError) as e:
        load_pdf(data)
    return e.value.code


def test_쪽_안에서_번호_제목마다_나누고_빈_쪽은_건너뛴다():
    r = load_pdf(REPORT)
    assert [p.location["page"] for p in r.pieces] == [1, 1, 2, 4]
    assert [p.context for p in r.pieces] == [None, "1. 개요", "2. 예산 계획", "3. 운영 조와 할 일"]
    assert r.pieces[1].location == {"type": "page", "page": 1, "label": "1쪽"}
    assert r.skipped == 1                                                # 3쪽은 빈 쪽
    assert {p.kind for p in r.pieces} == {"page"}


def test_머리글과_바닥글을_지운다():
    pieces = load_pdf(REPORT).pieces + load_pdf(PLAN).pieces
    lines = [line for p in pieces for line in p.content.split("\n")]
    assert "나인아웃팅 합성 자료 · 내부 검토용" not in lines
    assert not [line for line in lines if re.fullmatch(r"- \d+ -", line)]


def test_쪽을_넘어가는_표는_앞_쪽_제목을_이어받는다():
    pieces = load_pdf(PLAN).pieces
    page2 = [p for p in pieces if p.location["page"] == 2]
    assert page2[0].context == "2. 추진 일정 (준비 단계)" and page2[0].content.startswith("시기 할 일 담당 완료 기준")
    assert [p.context for p in page2[1:3]] == ["3. 당일 진행 순서 > 3.1 1일차: 11월 20일(목)",
                                              "3. 당일 진행 순서 > 3.2 2일차: 11월 21일(금)"]
    assert "3. 당일 진행 순서" not in [p.content for p in pieces]            # 제목 한 줄뿐인 조각은 넣지 않음


@pytest.mark.parametrize("line, level", [
    ("1. 행사 개요", 1), ("3.2 2일차: 11월 21일(금)", 2), ("10. 비상 대응", 1), ("1.2.3 세부", 3),
    ("2026 고객 초청 기술 세미나 운영계획서", None),        # 점이 없는 숫자
    ("1. 사전 등록자는 QR로 확인한다.", None),               # 마침표로 끝나는 문장
    ("3.5% 늘었다", None), ("15:30~15:50 휴식", None), ("- 1 -", None),
    ("1. " + "가" * 50, None),                              # 너무 긴 줄
])
def test_번호_제목_찾기(line, level):
    assert heading_level(line) == level


def test_머리글_바닥글은_여러_쪽에_되풀이될_때만_지운다():
    pages = [(1, ["회사 머리글", "예산 개요", "- 1 -"]), (2, ["회사 머리글", "운영 조 편성", "회사 머리글", "- 2 -"]),
             (3, ["장소 안내", "비상구 위치", "회사 머리글", "주차 안내", "마무리"])]
    out = dict(strip_header_footer(pages))
    assert out[1] == ["예산 개요"] and out[2] == ["운영 조 편성"]
    assert out[3] == ["장소 안내", "비상구 위치", "회사 머리글", "주차 안내", "마무리"]   # 위·아래 두 줄 밖은 남김
    assert strip_header_footer([(1, ["제목", "본문"])]) == [(1, ["제목", "본문"])]   # 한 쪽짜리는 그대로


def test_한글과_표를_글자_사이_띄어쓰기_없이_꺼낸다():
    """이 샘플은 글자 폭 정보가 어긋나 pypdf 로 읽으면 "나 인 아 웃 팅" 처럼 나온다 (make_upload_samples.py)"""
    title, _, budget, _ = load_pdf(REPORT).pieces
    assert title.content.startswith("2026년 4분기 고객 초청 행사 운영 계획")
    assert "마케팅 12,000,000원 온라인 광고 포함" in budget.content.split("\n")
    assert "\r" not in budget.content and "  " not in budget.content


def test_긴_쪽은_나뉘어도_같은_쪽_번호():
    chunks = split_pieces(load_pdf(REPORT).pieces, count_tokens=len, max_tokens=300)   # 글자 수를 토큰으로 친다
    page4 = [c for c in chunks if c.location["page"] == 4]
    assert len(page4) > 1 and all(len(c.embed_text) <= 300 for c in page4)


@pytest.mark.parametrize("name, code", [("upload_scan.pdf", NO_TEXT), ("upload_locked.pdf", ENCRYPTED)])
def test_글이_없거나_암호면_이유를_알린다(name, code):
    assert code_of((FIX / name).read_bytes()) == code


def test_암호_PDF는_형식_확인에서_먼저_걸린다():
    with pytest.raises(IngestError) as e:
        detect((FIX / "upload_locked.pdf").read_bytes())
    assert e.value.code == ENCRYPTED


def test_열_수_없는_PDF는_unsupported_format():
    assert code_of(b"%PDF-1.7\nbroken body") == UNSUPPORTED_FORMAT


def test_쪽_수_상한을_넘으면_too_large(monkeypatch):
    monkeypatch.setattr(pdf_loader, "MAX_PAGES", 3)
    assert code_of(REPORT) == TOO_LARGE


def test_공통_정리는_줄바꿈과_공백을_맞춘다():
    assert normalize("첫 줄\r\n  둘째\u3000\u3000줄  \r\r\n\n셋째\t줄") == "첫 줄\n둘째 줄\n셋째 줄"


def test_공통_정리는_자모로_쪼갠_한글을_합친다():
    nfd = unicodedata.normalize("NFD", "마케팅 예산")
    assert len(nfd) > len("마케팅 예산") and normalize(nfd) == "마케팅 예산"


def test_공통_정리는_보이지_않는_문자와_제어_문자를_뺀다():
    assert normalize("\ufeff마\u200b케\u00ad팅\x00 예\u200d산\x07") == "마케팅 예산"


def test_공통_정리는_원문_기호를_바꾸지_않는다():
    """NFKC 였다면 ①→1, ㈜→(주), 전각 숫자→반각 으로 바뀐다"""
    assert normalize("① ㈜나인아웃팅 １２３") == "① ㈜나인아웃팅 １２３"


def test_상세_운영계획서는_표와_긴_글을_순서대로_꺼낸다():
    """upload_plan.pdf (내용은 fixtures/plan_content.py). 검색 품질·처리 시간 측정(SCRUM-110)에 쓰는 샘플"""
    r = load_pdf((FIX / "upload_plan.pdf").read_bytes())
    text = "\n".join(p.content for p in r.pieces)
    assert len(r.pieces) >= 5 and r.skipped == 4             # 바로 아래 소제목이 오는 큰 제목 4개 ("1. 행사 개요" 등)
    for line in ("13:20~14:10 기조 발표: 회의 기록 자동화의 다음 단계 그랜드홀 발표자 1 질의응답 10분 포함",
                 "케이터링 6,750,000원 150명 × 2일, 1인 1식 22,500원",
                 "안전 책임자 1 안전 점검, 비상 대응 지휘, 인원 집계 양일 집결부터 철수까지"):
        assert line in text.split("\n")
    assert "견과류 알레르기 5명" in text and "나 인" not in text
