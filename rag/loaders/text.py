"""업로드 문서에서 꺼낸 글의 공통 정리. PDF·워드·엑셀 로더가 같이 쓴다.

  NFC 정규화        일부 PDF 는 한글을 자모로 쪼개 저장한다 ("한" = ㅎ+ㅏ+ㄴ). 합치지 않으면 같은 낱말이 검색에 안 걸린다.
                    NFKC 는 쓰지 않는다: ①→1, ㈜→(주) 처럼 원문을 바꾸는데, 답변 근거(citations.text)는 원문이어야 한다
  보이지 않는 문자   폭 없는 공백·이음표, 소프트 하이픈, BOM, 제어 문자를 뺀다 (줄바꿈·탭은 남김)
  공백              줄바꿈을 \\n 으로 맞추고, 줄마다 겹친 공백을 한 칸으로 줄이고, 빈 줄을 뺀다

머리글·바닥글·쪽 번호 빼기, 줄 이어 붙이기는 효과를 재 본 뒤 정한다 (SCRUM-110).
"""
import re
import unicodedata

_INVISIBLE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e]")   # 폭 없는 공백·이음표, BOM, 소프트 하이픈
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")                 # 제어 문자 (줄바꿈·탭은 남김)
_SPACES = re.compile("[ \t\u00a0\u3000\u2002-\u200a\u202f\u205f]+")             # 여러 종류의 공백


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", _INVISIBLE.sub("", text))
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)
