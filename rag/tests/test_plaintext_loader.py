"""텍스트(txt·md) 읽기 시험 (SCRUM-309). 합성 샘플은 fixtures/make_upload_samples.py --text 로 만든다."""
from pathlib import Path

import pytest

from rag.errors import NO_TEXT, TOO_LARGE, UNSUPPORTED_FORMAT, IngestError
from rag.filetype import TEXT, decode_text, detect
from rag.loaders import plaintext
from rag.loaders.docx import load_docx
from rag.loaders.plaintext import load_text

FIX = Path(__file__).parent / "fixtures"
PLAN_MD = (FIX / "upload_plan.md").read_bytes()
NOTES = (FIX / "upload_notes.txt").read_bytes()
NOTES_CP949 = (FIX / "upload_notes_cp949.txt").read_bytes()


def code_of(data):
    with pytest.raises(IngestError) as e:
        load_text(data)
    return e.value.code


def pieces(text):
    return load_text(text.encode()).pieces


# ---- 형식과 글자 인코딩 ----

def test_형식_확인에서_텍스트로_알아낸다():
    assert detect(PLAN_MD) == TEXT and detect(NOTES) == TEXT and detect(NOTES_CP949) == TEXT
    assert detect("PKI 인증서 정리".encode()) == TEXT          # "PK" 로 시작해도 zip 이 아니면 글


def test_UTF8_CP949_UTF16_모두_같은_글로_읽는다():
    assert load_text(NOTES).pieces == load_text(NOTES_CP949).pieces
    text = "회의 메모\n둘째 줄"
    assert decode_text(text.encode("utf-16")) == text                       # BOM 이 붙는 윈도우 메모장 "유니코드"
    assert decode_text(b"\xef\xbb\xbf" + text.encode()) == text             # UTF-8 BOM 은 뗀다
    assert [p.content for p in load_text(b"\xfe\xff" + text.encode("utf-16-be")).pieces] == [text]   # 빅 엔디언 BOM


@pytest.mark.parametrize("data", [
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",       # 이미지 (0바이트가 있음)
    "글자".encode("utf-16-le"),                    # BOM 없는 UTF-16 (0바이트가 있음)
    bytes(range(1, 32)) * 10,                      # 0바이트는 없지만 제어 문자뿐
])
def test_글자로_읽히지_않으면_unsupported_format(data):
    assert decode_text(data) is None
    assert code_of(data) == UNSUPPORTED_FORMAT
    with pytest.raises(IngestError) as e:
        detect(data)
    assert e.value.code == UNSUPPORTED_FORMAT


def test_빈_파일과_공백뿐인_파일은_no_text():
    assert detect(b"") == TEXT
    assert code_of(b"") == NO_TEXT
    assert code_of(" \r\n\t\n  \n".encode()) == NO_TEXT
    assert code_of("```\n```\n".encode()) == NO_TEXT


def test_글자_수가_상한을_넘으면_too_large(monkeypatch):
    monkeypatch.setattr(plaintext, "MAX_CHARS", 10)
    assert code_of(("가" * 11).encode()) == TOO_LARGE
    assert pieces("가" * 10)


# ---- 마크다운: 제목으로 나누기 ----

def test_마크다운은_워드와_같은_위치와_제목_경로로_나뉜다():
    """같은 운영계획서의 워드와 비교. 다른 것은 목록 기호("- ")를 원문대로 둔 것뿐이다."""
    md, docx = load_text(PLAN_MD), load_docx((FIX / "upload_plan.docx").read_bytes())
    assert len(md.pieces) == len(docx.pieces) == 29 and md.skipped == docx.skipped
    for a, b in zip(md.pieces, docx.pieces):
        assert (a.location, a.context, a.kind) == (b.location, b.context, b.kind)
        assert "\n".join(line.removeprefix("- ") for line in a.content.split("\n")) == b.content


def test_마크다운_표는_행마다_열_이름을_붙인_한_줄():
    lines = "\n".join(p.content for p in load_text(PLAN_MD).pieces).split("\n")
    assert "항목: 케이터링 | 금액: 6,750,000원 | 산출 근거: 150명 × 2일, 1인 1식 22,500원" in lines
    assert not any(set(line) <= set("|-: ") for line in lines)              # 구분 줄(|---|)은 빠진다


def test_제목_전_글은_문서_처음_코드_블록_안의_샵은_제목이_아니다():
    start, real = pieces("머리말\n```\n# 주석이다\n```\n## 진짜 제목 ##\n본문")
    assert start.location == {"type": "section", "heading": "", "label": "문서 처음"} and start.context is None
    assert start.content == "머리말\n# 주석이다"
    assert real.location["heading"] == "진짜 제목" and real.content == "진짜 제목\n본문"


def test_링크와_그림은_글만_남기고_다른_표시는_그대로():
    (p,) = pieces("# 안내\n자세한 것은 [운영 문서](https://example.com/a) 참고 ![지도](map.png)\n- **굵게** 쓴 목록")
    assert p.content == "안내\n자세한 것은 운영 문서 참고 지도\n- **굵게** 쓴 목록"


def test_구분_줄이_없으면_표가_아니다():
    (p,) = pieces("# 메모\n가 | 나\n다 | 라\n가 | 나\n---")
    assert p.content == "메모\n가 | 나\n다 | 라\n가 | 나\n---"


# ---- 제목이 없는 글: 문단으로 나누기, 위치는 줄 번호 ----

def test_제목이_없는_글은_줄_번호로_위치를_준다():
    (p,) = load_text(NOTES).pieces                       # 5문단 309자 → 한 조각 (윈도우 줄바꿈도 줄 하나로 센다)
    assert p.location == {"type": "line", "lines": [1, 10], "label": "1~10줄"}
    assert p.kind == "line" and p.context is None
    assert p.content.startswith("11월 정기 점검 메모 (합성 자료)\n회의실 프로젝터")


def test_짧은_문단은_GROUP_CHARS_까지_잇고_넘으면_새_조각():
    para = "가" * 150
    out = pieces("\n\n".join([para] * 4))                 # 문단은 1·3·5·7줄. 둘이면 301자, 셋이면 452자
    assert [p.location["lines"] for p in out] == [[1, 3], [5, 7]]
    assert out[0].content == para + "\n" + para


def test_긴_문단은_줄_단위로_끊고_한_줄이_길면_그_줄만():
    line = "나" * 99                                       # 줄 4개가 399자
    out = pieces("\n".join([line] * 9) + "\n\n" + "다" * 1000 + "\n\n짧은 끝")
    assert [p.location["label"] for p in out] == ["1~4줄", "5~8줄", "9줄", "11줄", "13줄"]
