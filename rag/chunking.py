"""Piece -> 임베딩 입력 상한 안에 들어가는 Chunk.

  - 상한 안이면 Piece 하나가 Chunk 하나
  - 넘으면 문장 경계(. ? ! 。 뒤 공백, 줄바꿈)에서 앞에서부터 채워 나눈다. 나뉜 조각은 같은 location 을 가진다
  - 문장 하나가 넘으면 글자 위치에서 자른다 (토큰 수가 상한 안인 가장 긴 앞부분)
토큰 수는 count_tokens 로 센다. 실제로는 임베더의 passage_tokens (모델 접두어·특수 토큰 포함)를 넘긴다.
"""
import copy
import re
from typing import Callable, Iterable

from .errors import INTERNAL_ERROR, IngestError
from .models import Chunk, Piece

# "3.5" 처럼 마침표 뒤에 공백이 없으면 문장 끝으로 보지 않는다
_SENTENCE = re.compile(r".*?(?:[.?!。](?=\s|$)|\n|$)\s*", re.S)


def embed_text_of(context, content):
    return f"[{context}] {content}" if context else content


def split_pieces(pieces: Iterable[Piece], count_tokens: Callable[[str], int], max_tokens: int) -> list[Chunk]:
    chunks = []
    for piece in pieces:
        for part in _split(piece, count_tokens, max_tokens):
            text = embed_text_of(piece.context, part)
            chunks.append(Chunk(chunk_index=len(chunks), content=part, embed_text=text,
                                location=copy.deepcopy(piece.location),
                                token_count=count_tokens(text), kind=piece.kind))
    return chunks


def _split(piece, count_tokens, max_tokens):
    def fits(s):
        return count_tokens(embed_text_of(piece.context, s)) <= max_tokens

    if fits(piece.content):
        return [piece.content]

    parts, cur = [], ""
    for sent in _sentences(piece.content):
        if fits(cur + sent):
            cur += sent
            continue
        if cur.strip():
            parts.append(cur.strip())
        if fits(sent):
            cur = sent
        else:
            *done, cur = _hard_split(sent, fits)
            parts.extend(done)
    if cur.strip():
        parts.append(cur.strip())
    return parts


def _sentences(text):
    return [s for s in _SENTENCE.findall(text) if s]


def _hard_split(text, fits):
    """fits 를 만족하는 가장 긴 앞부분씩 자른다. 마지막 조각도 fits 를 만족한다.
    마지막 조각은 뒤 공백을 남긴다 (다음 문장과 이어 붙일 때 원문 공백을 지키려고)."""
    out, rest = [], text.lstrip()
    while rest:
        if fits(rest):
            out.append(rest)
            break
        if not fits(rest[:1]):
            raise IngestError(INTERNAL_ERROR, "맥락(주제)만으로 토큰 상한을 넘음")
        lo, hi = 1, len(rest) - 1          # fits(rest[:lo]) 는 참, fits(rest) 는 거짓
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fits(rest[:mid]):
                lo = mid
            else:
                hi = mid - 1
        head = rest[:lo].strip()
        if head:
            out.append(head)
        rest = rest[lo:].lstrip()
    return out
