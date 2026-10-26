"""PDF 읽기 시험 (SCRUM-107). 합성 샘플은 fixtures/make_upload_samples.py 로 만든다."""
import unicodedata
from pathlib import Path

import pytest

from rag.chunking import split_pieces
from rag.errors import ENCRYPTED, NO_TEXT, TOO_LARGE, UNSUPPORTED_FORMAT, IngestError
from rag.filetype import detect
from rag.loaders import pdf as pdf_loader
from rag.loaders.pdf import load_pdf
from rag.loaders.text import normalize

FIX = Path(__file__).parent / "fixtures"
REPORT = (FIX / "upload_report.pdf").read_bytes()


def code_of(data):
    with pytest.raises(IngestError) as e:
        load_pdf(data)
    return e.value.code


def test_쪽마다_조각_하나_빈_쪽은_건너뛴다():
    r = load_pdf(REPORT)
    assert [p.location for p in r.pieces] == [{"type": "page", "page": n, "label": f"{n}쪽"} for n in (1, 2, 4)]
    assert r.skipped == 1                                                # 3쪽은 빈 쪽
    assert {p.kind for p in r.pieces} == {"page"}


def test_한글과_표를_글자_사이_띄어쓰기_없이_꺼낸다():
    """이 샘플은 글자 폭 정보가 어긋나 pypdf 로 읽으면 "나 인 아 웃 팅" 처럼 나온다 (make_upload_samples.py)"""
    page1, page2, _ = load_pdf(REPORT).pieces
    assert page1.content.startswith("나인아웃팅 합성 자료 · 내부 검토용\n2026년 4분기 고객 초청 행사 운영 계획")
    assert "마케팅 12,000,000원 온라인 광고 포함" in page2.content.split("\n")
    assert "\r" not in page2.content and "  " not in page2.content


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
    assert len(r.pieces) >= 5 and r.skipped == 0
    for line in ("13:20~14:10 기조 발표: 회의 기록 자동화의 다음 단계 그랜드홀 발표자 1 질의응답 10분 포함",
                 "케이터링 6,750,000원 150명 × 2일, 1인 1식 22,500원",
                 "안전 책임자 1 안전 점검, 비상 대응 지휘, 인원 집계 양일 집결부터 철수까지"):
        assert line in text.split("\n")
    assert "견과류 알레르기 5명" in text and "나 인" not in text
