"""나누기 시험. 토큰 수 대신 글자 수를 세는 가짜 계산기를 쓴다 (모델 없이 돈다)."""
import pytest

from rag.chunking import split_pieces
from rag.errors import INTERNAL_ERROR, IngestError
from rag.models import Piece

LOC = {"type": "time", "startMs": 0, "endMs": 1000, "label": "00:00~00:01"}


def count_chars(text):
    return len("passage: " + text)                         # 접두어까지 센다


def test_상한_안이면_그대로_하나():
    chunks = split_pieces([Piece("짧은 글", LOC, context="도구", kind="summary")], count_chars, 50)
    assert len(chunks) == 1
    c = chunks[0]
    assert (c.chunk_index, c.content, c.embed_text, c.kind) == (0, "짧은 글", "[도구] 짧은 글", "summary")
    assert c.token_count == count_chars("[도구] 짧은 글")


def test_문장_경계에서_나누고_원문을_잃지_않음():
    text = "첫째 문장입니다. 둘째 문장이에요? 셋째 문장!\n넷째 줄"
    chunks = split_pieces([Piece(text, LOC)], count_chars, 30)
    assert [c.content for c in chunks] == ["첫째 문장입니다. 둘째 문장이에요?", "셋째 문장!\n넷째 줄"]
    assert all(c.token_count <= 30 for c in chunks)


def test_소수점은_문장_끝이_아님():
    text = "금액은 3.5만 원입니다. 다음"
    chunks = split_pieces([Piece(text, LOC)], count_chars, 25)
    assert [c.content for c in chunks] == ["금액은 3.5만 원입니다.", "다음"]


def test_너무_긴_문장은_글자_위치에서_자름():
    text = "가" * 50
    chunks = split_pieces([Piece(text, LOC)], count_chars, 20)
    assert "".join(c.content for c in chunks) == text
    assert all(c.token_count <= 20 for c in chunks)
    assert [len(c.content) for c in chunks] == [11, 11, 11, 11, 6]


def test_긴_문장_자투리는_다음_문장과_이어_붙음():
    text = "가" * 25 + ". 나."
    chunks = split_pieces([Piece(text, LOC)], count_chars, 20)
    assert all(c.token_count <= 20 for c in chunks)
    assert [c.content for c in chunks] == ["가" * 11, "가" * 11, "가가가. 나."]   # 원문 공백 유지


def test_location_은_복사본이고_chunk_index_는_문서_전체_순번():
    pieces = [Piece("가. " * 10, LOC), Piece("짧음", LOC)]
    chunks = split_pieces(pieces, count_chars, 20)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(c.location == LOC for c in chunks)
    chunks[0].location["label"] = "바뀜"
    assert LOC["label"] == "00:00~00:01" and chunks[1].location["label"] == "00:00~00:01"


def test_맥락만으로_상한을_넘으면_오류():
    with pytest.raises(IngestError) as e:
        split_pieces([Piece("본문이 조금 깁니다", LOC, context="아주 긴 주제" * 5)], count_chars, 20)
    assert e.value.code == INTERNAL_ERROR
