"""AI 가 채우는 표 필드 (명세 7.6). 배치가 끝난 뒤 표 필드마다 AI 를 한 번 더 불러 열 이름과 칸 값을 정한다.

  AI 에게 주는 글   <필드> 이름, <설명>(있으면), <열 개수>, <재료> m 번호마다 이름 붙은 조각 (materials.py 의 parts)
  AI 의 답          {"columns": [...], "rows": [{"m", "cells": [...]}]}
  검사              모양, 열 수·이름, 재료마다 한 행, 칸 수, 칸 값이 그 재료의 조각에 있는지, STT 표시, 빈 행,
                    내용 없는 행(안건·종류뿐), | 와 줄바꿈
  다시 시키기       배치와 같다 (placement.ask)

배치와 한 번에 하지 않는 것은 실제 요약으로 맞춰 둔 배치 품질을 흔들지 않기 위해서다.
칸 값은 조각을 그대로 쓰거나 잘라 쓴 글만 받는다 (띄어쓰기 차이는 허용). 바꿔 쓴 글이나 조각에 없는 숫자·이름을 막기 위해서다.
행의 순서와 근거·표시·출처는 코드가 재료에서 정한다 (lines.py). 검사 오류 글에는 칸 값이 들어가므로 AI 에게만 보내고 로그에 남기지 않는다.
"""
import re

from .models import Field, Material, Table
from .placement import Outcome, Prompt, _one_line, _quote, ask, load_prompt

TABLE_KIND = "table"
MAX_COLUMNS = 6
MAX_COLUMN_NAME = 30
_SPACE = re.compile(r"\s+")
_QUOTED = re.compile(r"「([^」]*)」")
LABEL_PARTS = ("안건", "종류", "표시")     # 내용이 아닌 조각. 이것만으로 된 행은 내용이 없다


def build_user(field: Field, materials: list[Material], problems: list[str] = ()) -> str:
    """AI 에게 줄 글. 꺾쇠는 글 안에서 바꿔 칸 밖으로 새지 않게 한다 (배치와 같음)."""
    parts = [f"<필드>{_quote(field.title)}</필드>"]
    if field.description.strip():
        parts.append(f"<설명>{_quote(_one_line(field.description.strip()))}</설명>")
    count = str(field.columns) if field.columns else f"1~{MAX_COLUMNS} 중에서 정함"
    parts += [f"<열 개수>{count}</열 개수>", "<재료>"]
    for m in materials:
        parts.append(m.ref)
        parts += [f"  {name}: {_quote(_one_line(value))}" for name, value in m.parts]
    parts.append("</재료>")
    if problems:
        parts += ["<지난 답의 문제>", *(f"- {p}" for p in problems), "</지난 답의 문제>"]
    return "\n".join(parts)


def check(data, materials: list[Material], columns: int | None) -> tuple[Table | None, list[str]]:
    """(표, 문제 목록). 문제가 하나라도 있으면 표는 None."""
    if not isinstance(data, dict) or not isinstance(data.get("columns"), list) or not isinstance(data.get("rows"), list):
        return None, ['답이 {"columns": [...], "rows": [...]} 모양의 JSON이 아님']
    names = data["columns"]
    problems = _column_problems(names, columns)
    by_ref = {m.ref: m for m in materials}
    rows, seen = {}, {}
    for row in data["rows"]:
        if not isinstance(row, dict) or not isinstance(row.get("m"), str) or not isinstance(row.get("cells"), list):
            problems.append('rows의 항목이 {"m": 재료 번호, "cells": [...]} 모양이 아님')
            continue
        ref, cells = row["m"], row["cells"]
        if ref not in by_ref:
            problems.append(f"없는 재료 번호 {ref}")
            continue
        seen[ref] = seen.get(ref, 0) + 1
        found = _row_problems(by_ref[ref], cells, names)
        problems += found
        if not found:
            rows[ref] = tuple(c.strip() for c in cells)
    problems += [f"{ref}을 두 번 이상 씀" for ref, n in seen.items() if n > 1]
    problems += [f"{m.ref}의 행이 없음" for m in materials if m.ref not in seen]
    if problems:
        return None, problems
    return Table(columns=tuple(n.strip() for n in names), rows=rows), []


def fill(field: Field, materials: list[Material], llm, timeout_s: float, max_retries: int = 2,
         prompt: Prompt | None = None, call_retries: int = 1) -> Outcome:
    """그 표 필드에 놓인 재료(1개 이상)로 표를 채운다. 성공하면 Outcome.placement 가 Table 이다."""
    return ask(prompt or load_prompt(kind=TABLE_KIND), lambda problems: build_user(field, materials, problems),
               lambda data: check(data, materials, field.columns), llm, timeout_s, max_retries, call_retries)


def markdown(table: Table, rows: list[tuple[str, ...]]) -> str:
    """마크다운 표 (명세 4.1·5.4). 칸 글에는 | 와 줄바꿈이 없다 (검사)."""
    lines = [_md_row(table.columns), "|" + "---|" * len(table.columns)]
    return "\n".join(lines + [_md_row(cells) for cells in rows])


def _md_row(cells) -> str:
    return "| " + " | ".join(cells) + " |"


def _column_problems(names, columns) -> list[str]:
    problems = []
    if columns and len(names) != columns:
        problems.append(f"열이 {columns}개가 아니라 {len(names)}개임")
    elif not columns and not 1 <= len(names) <= MAX_COLUMNS:
        problems.append(f"열이 1~{MAX_COLUMNS}개가 아님 ({len(names)}개)")
    seen = set()
    for i, name in enumerate(names, 1):
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{i}번째 열 이름이 비었거나 글이 아님")
            continue
        if len(name.strip()) > MAX_COLUMN_NAME:
            problems.append(f"{i}번째 열 이름이 {MAX_COLUMN_NAME}자를 넘음")
        if _bad_char(name):
            problems.append(f"{i}번째 열 이름에 | 나 줄바꿈이 있음")
        if name.strip() in seen:
            problems.append(f'열 이름 "{name.strip()}"이 두 번 나옴')
        seen.add(name.strip())
    return problems


def _row_problems(m: Material, cells: list, names: list) -> list[str]:
    if len(cells) != len(names):
        return [f"{m.ref}의 칸이 {len(names)}개가 아니라 {len(cells)}개임"]
    if any(not isinstance(c, str) for c in cells):
        return [f"{m.ref}의 칸 값이 모두 글이어야 함"]
    if not any(c.strip() for c in cells):
        return [f"{m.ref}의 칸이 모두 비었음"]
    problems = []
    for name, cell in zip(names, cells):
        column = name.strip() if isinstance(name, str) else name
        if _bad_char(cell):
            problems.append(f'{m.ref}의 "{column}" 칸에 | 나 줄바꿈이 있음')
            continue
        if not cell.strip():
            continue
        holders = [value for _, value in m.parts if _norm(cell) in _norm(value)]
        if not holders:
            problems.append(f'{m.ref}의 "{column}" 칸 "{cell.strip()}"이 재료 조각에 없음 (조각 글을 그대로 쓰거나 잘라 써야 함)')
        elif all(_drops_mark(cell, value) for value in holders):
            problems.append(f'{m.ref}의 "{column}" 칸에서 「」 표시가 빠짐 (표시까지 함께 써야 함)')
    contents = [value for name, value in m.parts if name not in LABEL_PARTS]
    if not problems and not any(c.strip() and _norm(c) in _norm(v) for c in cells for v in contents):
        # 실제 측정 (2026-10-29, gpt-5.6-luna): 종류가 섞인 표에서 공통 조각인 안건·종류만 열로 삼아 "예산 | 미결" 같은 표가 나옴
        problems.append(f"{m.ref}의 행에 안건·종류·표시 말고 내용이 없음 ({_content_names(m)} 중 하나를 칸에 넣어야 함)")
    return problems


def _content_names(m: Material) -> str:
    return ", ".join(name for name, _ in m.parts if name not in LABEL_PARTS)


def _drops_mark(cell: str, value: str) -> bool:
    """조각의 「…」 안 글을 칸에 쓰면서 「」 를 뺐는가. 요약 단계가 남긴 STT 불확실 표시를 지우지 않게 한다."""
    c = _norm(cell)
    return any(_norm(inner) and _norm(inner) in c and _norm(f"「{inner}」") not in c for inner in _QUOTED.findall(value))


def _norm(text: str) -> str:
    return _SPACE.sub("", text)


def _bad_char(text: str) -> bool:
    return "|" in text or "\n" in text or "\r" in text
