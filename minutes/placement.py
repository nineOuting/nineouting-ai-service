"""AI 에게 칸 고르기와 글 다듬기를 묻고, 답을 검사한다. 명세 7.2·7.3

  AI 에게 주는 글   <칸> 번호 붙인 칸 이름, <재료> m 번호·안건·종류·(다듬기/고정)·글. 다시 배치의 새로 쓴 줄은 안건 없이 [직접 작성]
  AI 의 답          {"placements": [{"m", "section", "text"}], "unplaced": [...]}
  검사              형식, 칸 번호, 재료 번호, 한 번만, 빠짐, 다듬은 글(빈 글·줄바꿈·새 숫자)
  다시 시키기       검사에 걸리면 걸린 이유를 붙여 다시 묻는다 (max_retries 번). 넘으면 CHECK_FAILED
  호출 실패         같은 입력으로 call_retries 번(기본 1번) 다시 부른다. 그래도 실패하면 마지막 실패가
                    시간 초과면 LLM_TIMEOUT, 그 밖은 LLM_FAILED. 초안은 접수 후 콜백으로 알리므로
                    사용자가 화면에서 기다리는 RAG 답변(다시 부르지 않음)과 달리 한 번 더 시도한다

검사 오류 글에는 재료 번호와 숫자가 들어간다. AI 에게만 보내고 로그에 남기지 않는다.
"""
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rag.llm import LLMError, LLMTimeout

from .models import Material, Placement

PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPT_KIND = "placement"
RELAYOUT_KIND = "relayout"      # 양식 변경 시 다시 배치 (명세 8장). 처음 초안과 지시문을 나눈 이유는 prompts/relayout/v1.md 머리
CHECK_FAILED = "CHECK_FAILED"
LLM_TIMEOUT = "LLM_TIMEOUT"
LLM_FAILED = "LLM_FAILED"
_VERSION = re.compile(r"v\d+")
_NUMBER = re.compile(r"\d+")


class PromptError(ValueError):
    """지시문 파일이 없거나 모양이 틀림"""


@dataclass(frozen=True)
class Prompt:
    version: str              # "placement-v1". 로그에 남는다
    text: str


@dataclass(frozen=True)
class Outcome:
    placement: Placement | None     # 성공이면 배치, 실패면 None
    error_code: str | None          # CHECK_FAILED / LLM_TIMEOUT / LLM_FAILED
    attempts: int                   # AI 를 부른 횟수
    input_tokens: int = 0
    output_tokens: int = 0


@lru_cache(maxsize=None)
def load_prompt(version: str | None = None, kind: str = PROMPT_KIND) -> Prompt:
    """minutes/prompts/{kind}/{version}.md. version 이 없으면 번호가 가장 큰 파일.
    맨 위 --- 사이의 version 이 "{kind}-{version}" 과 같아야 한다 (RAG 지시문과 같은 규칙)."""
    folder = PROMPTS_DIR / kind
    if version is None:
        names = sorted((p.stem for p in folder.glob("v*.md") if _VERSION.fullmatch(p.stem)), key=lambda v: int(v[1:]))
        if not names:
            raise PromptError(f"지시문 파일 없음: prompts/{kind}/v*.md")
        version = names[-1]
    if not _VERSION.fullmatch(version):
        raise PromptError(f"지시문 버전은 v1, v2 처럼 써야 함 (받은 값: {version})")
    try:
        raw = (folder / f"{version}.md").read_text(encoding="utf-8")
    except OSError:
        raise PromptError(f"지시문 파일 없음: prompts/{kind}/{version}.md") from None
    m = re.match(r"---\n(.*?)\n---\n(.*)", raw, re.S)
    expected = f"{kind}-{version}"
    meta = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line) if m else {}
    if meta.get("version", "").strip() != expected or not m.group(2).strip():
        raise PromptError(f"prompts/{kind}/{version}.md 맨 위의 version 이 {expected} 가 아니거나 본문이 없음")
    return Prompt(expected, m.group(2).strip())


def build_user(items: list[str], materials: list[Material], problems: list[str] = ()) -> str:
    """AI 에게 줄 글. 칸은 1번부터, 재료는 m 번호로 가리킨다. 꺾쇠는 글 안에서 바꿔 칸 밖으로 새지 않게 한다."""
    parts = ["<칸>", *(f"{i}. {_quote(name)}" for i, name in enumerate(items, 1)), "</칸>", "<재료>"]
    for m in materials:
        how = "다듬기" if m.rewrite else "고정"
        head = f"{_quote(m.agenda_title)} / {m.label}" if m.agenda_title else m.label    # 작성자가 새로 쓴 줄은 안건이 없다
        parts.append(f"{m.ref} [{head}] ({how}) {_quote(m.text)}")
    parts.append("</재료>")
    if problems:
        parts += ["<지난 답의 문제>", *(f"- {p}" for p in problems), "</지난 답의 문제>"]
    return "\n".join(parts)


def _quote(text) -> str:
    return str(text).replace("<", "‹").replace(">", "›")


def parse_reply(text: str):
    """답에서 JSON 객체를 꺼낸다. ```json 울타리나 앞뒤 말이 붙어도 첫 { 부터 마지막 } 까지 읽는다.
    읽을 수 없으면 None."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


def check(data, materials: list[Material], n_sections: int) -> tuple[Placement | None, list[str]]:
    """(배치, 문제 목록). 문제가 하나라도 있으면 배치는 None."""
    if not isinstance(data, dict) or not isinstance(data.get("placements"), list):
        return None, ['답이 {"placements": [...], "unplaced": [...]} 모양의 JSON이 아님']
    unplaced = data.get("unplaced", [])
    if not isinstance(unplaced, list):
        return None, ["unplaced가 목록이 아님"]

    by_ref = {m.ref: m for m in materials}
    problems, seen = [], {}
    sections, texts = {}, {}

    def note(ref):
        seen[ref] = seen.get(ref, 0) + 1

    for p in data["placements"]:
        if not isinstance(p, dict) or not isinstance(p.get("m"), str):
            problems.append("placements의 항목에 m(재료 번호)이 없음")
            continue
        ref, section = p["m"], p.get("section")
        if ref not in by_ref:
            problems.append(f"없는 재료 번호 {ref}")
            continue
        note(ref)
        if isinstance(section, bool) or not isinstance(section, int) or not 1 <= section <= n_sections:
            problems.append(f"{ref}의 section이 1~{n_sections} 사이의 칸 번호가 아님")
            continue
        sections[ref] = section - 1
        m = by_ref[ref]
        if m.rewrite:
            text = p.get("text")
            problem = _text_problem(m, text)
            if problem:
                problems.append(problem)
            else:
                texts[ref] = text.strip()
    for ref in unplaced:
        if ref not in by_ref:
            problems.append(f"unplaced에 없는 재료 번호 {ref}")
            continue
        note(ref)

    problems += [f"{ref}를 두 번 이상 씀" for ref, n in seen.items() if n > 1]
    problems += [f"{m.ref}가 placements에도 unplaced에도 없음" for m in materials if m.ref not in seen]
    if problems:
        return None, problems
    return Placement(sections=sections, texts=texts, unplaced=tuple(r for r in unplaced)), []


def _text_problem(m: Material, text) -> str | None:
    """다듬기 재료의 글 검사. 원래 글에 없는 숫자가 있으면 지어낸 것으로 본다 (한글로 쓴 숫자는 잡지 못한다)."""
    if not isinstance(text, str) or not text.strip():
        return f"{m.ref}는 (다듬기) 재료인데 text가 비었음"
    if "\n" in text.strip():
        return f"{m.ref}의 text에 줄바꿈이 있음"
    new = sorted(set(_NUMBER.findall(text)) - set(_NUMBER.findall(m.text)), key=int)
    if new:
        return f"{m.ref}의 text에 원래 글에 없는 숫자 {', '.join(new)}가 있음"
    return None


def place(items: list[str], materials: list[Material], llm, timeout_s: float, max_retries: int = 2,
          prompt: Prompt | None = None, call_retries: int = 1) -> Outcome:
    """재료가 1개 이상일 때 부른다. 검사에 걸리면 max_retries 번 다시 시키고,
    호출이 실패하면(시간 초과·오류) 같은 입력으로 call_retries 번 다시 부른다. 두 횟수는 따로 센다."""
    prompt = prompt or load_prompt()
    problems, attempts, tokens_in, tokens_out = [], 0, 0, 0
    checks = call_failures = 0
    while checks <= max_retries:
        attempts += 1
        try:
            reply = llm.generate(prompt.text, build_user(items, materials, problems), timeout_s)
        except LLMError as e:                                # LLMTimeout 도 여기에 걸린다
            call_failures += 1
            if call_failures > call_retries:
                code = LLM_TIMEOUT if isinstance(e, LLMTimeout) else LLM_FAILED
                return Outcome(None, code, attempts, tokens_in, tokens_out)
            continue
        checks += 1
        tokens_in += reply.input_tokens or 0
        tokens_out += reply.output_tokens or 0
        placement, problems = check(parse_reply(reply.text), materials, len(items))
        if placement is not None:
            return Outcome(placement, None, attempts, tokens_in, tokens_out)
    return Outcome(None, CHECK_FAILED, attempts, tokens_in, tokens_out)
