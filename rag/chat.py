"""회의 중 챗봇. 명세: docs/api/rag-chat-contract.md

  ① 질문 고쳐 쓰기   history 가 있을 때만 AI 를 한 번 부른다. 실패하거나 답이 이상하면 원래 질문을 쓴다
  ② 근거 모으기      출처마다 따로, AI 없이. 출처마다 Evidence 로 바꾼다
                     지금은 회의록·첨부 자료 검색 하나 (SCRUM-223). 진행 중 회의(안건 보드, 최근 전사 줄)는 SCRUM-224
  ③ 합치기           모은 근거에 [1]..[n] 번호를 붙인다
  ④ 답하기           AI 를 한 번 부른다. <대화>는 질문의 뜻을 알아내는 데만 쓰고, 근거는 <자료>만 쓴다
  ⑤ 판정             답변 API 와 같은 judge_reply. basis 는 지금 늘 sources (일반 지식 [일반] 은 SCRUM-225)

출처가 늘어도 AI 호출은 많아야 두 번이다 (고쳐 쓰기 1 + 답하기 1). 차례로 찾지 않고 한꺼번에 모으는 이유:
검색은 관련 없는 질문에도 상위 k 개를 돌려주므로 "없으면 다음 출처"를 AI 없이 정할 수 없고,
지난 회의록과 진행 중 회의가 엇갈릴 때 둘 다 봐야 최근 내용으로 답할 수 있다.

시간 (명세 v0.2 4.4): 요청 하나는 최대 60초. 고쳐 쓰기는 기본 모델만 최대 5초(폴백 없음, 넘으면 원래 질문),
답하기는 기본 모델 최대 25초, 실패하면 남은 시간이 10초 이상일 때만 폴백 모델을 남은 시간 안에서 부른다.
그래서 공용 FallbackLLM 을 그대로 부르지 않고 기본·폴백 모델을 꺼내 직접 부른다 (FallbackLLM 의 "실패한 기본 모델 건너뛰기"는
쓰지 않는다. 장애 중에는 질문마다 기본 모델을 25초까지 기다린 뒤 폴백으로 간다. 측정 뒤 넣을지 정한다).
문서는 (sourceType, docId) 짝으로 구분한다 (명세 v0.2 9장).

앞선 챗봇 답의 [n]·[일반] 표시는 지우고 넘긴다. 그때의 근거 번호라 이번 번호와 헷갈린다.
지금(SCRUM-223)은 docs 가 비었거나 찾은 근거가 없으면 AI 를 부르지 않고 no_result 다 (일반 지식이 생기면 바뀐다).
로그에는 질문·대화·답·근거 글을 남기지 않는다 (ID, 개수, 상태, 오류 코드, 걸린 시간, 토큰 수만).
"""
import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Callable, ContextManager

from shared.llm import FallbackLLM, LLMError, LLMTimeout, label
from .answer import LLM_FAILED, LLM_TIMEOUT, NO_ANSWER, Prompt, PromptError, judge_reply, load_prompt
from .search import search

log = logging.getLogger("rag.chat")

CHAT_KIND = "chat"
REWRITE_KIND = "rewrite"
NOT_FOUND = "회의 자료에서 찾을 수 없어요"     # no_result 의 answer (명세 5장)
HISTORY_TURNS = 6                            # 고쳐 쓰기·답하기에 넘기는 최근 대화 수 (3번 주고받기, 정해 둔 값)
REWRITE_MAX = 300                            # 고친 질문이 이보다 길면 버린다
DOC_TOP_K = 5

ROLES = {"user": "사용자", "assistant": "챗봇"}
_MARKS = re.compile(r"\s*\[(?:\d+(?:\s*,\s*\d+)*|일반)\]")


# ---------- 요청과 근거 ----------

@dataclass(frozen=True)
class Turn:
    role: str                    # user / assistant
    text: str


@dataclass(frozen=True)
class ChatRequest:
    workspace_id: int
    meeting_id: int
    user_id: int
    query: str
    docs: tuple[tuple[str, int], ...]   # (sourceType, docId) 짝
    history: tuple[Turn, ...] = ()
    user_name: str | None = None
    current_agenda_id: int | str | None = None
    live_agendas: tuple = ()     # 형식 검사만 한다. 쓰는 것은 SCRUM-224
    recent_lines: tuple = ()     # 위와 같음


@dataclass(frozen=True)
class Evidence:
    """근거 하나. 출처와 관계없이 같은 모양이라 ③~⑤는 출처를 모른다."""
    source_type: str             # minutes / upload / live
    doc_id: int | None
    title: str                   # 문서 제목 / "진행 중 회의"
    text: str                    # AI 에 넘기고 citations[].text 로 나가는 글
    location: dict = field(default_factory=dict)

    def citation(self, ref: int) -> dict:
        return {"ref": ref, "sourceType": self.source_type, "docId": self.doc_id, "title": self.title,
                "text": self.text, "location": self.location}


def doc_evidence(connection: Callable[[], ContextManager], embedder, workspace_id, docs, query,
                 top_k=DOC_TOP_K) -> list[Evidence]:
    """출처: 지난 회의록·첨부 자료. 답변 API 와 같은 검색 (workspaceId + docs 짝). docs 가 비면 DB 를 열지 않는다."""
    if not docs:
        return []
    with connection() as conn:
        hits = search(conn, embedder, workspace_id, docs, query, top_k)
    return [Evidence(h.source_type, h.doc_id, h.title, h.content, h.location) for h in hits]


# ---------- AI 에 넘기는 글 ----------

def strip_marks(text: str) -> str:
    """챗봇 답의 [1]·[1, 2]·[일반] 표시를 지운다."""
    return _MARKS.sub("", text).strip()


def recent_turns(history, n=HISTORY_TURNS) -> list[Turn]:
    return [Turn(t.role, strip_marks(t.text)) for t in list(history)[-n:]]


def _quote(text) -> str:
    """칸을 닫는 꺾쇠를 글 안에서 바꿔 칸 밖으로 새지 않게 한다 (answer.build_prompt 와 같은 처리)."""
    return str(text).replace("<", "‹").replace(">", "›")


def conversation_block(turns) -> str:
    lines = [f"{ROLES.get(t.role, t.role)}: {_quote(t.text)}" for t in turns]
    return "<대화>\n" + "\n".join(lines) + "\n</대화>"


def rewrite_input(turns, query) -> str:
    return f"{conversation_block(turns)}\n\n<질문>\n{_quote(query)}\n</질문>"


def chat_input(query, turns, evidence, rewritten=None) -> str:
    """조각 머리는 답변 API 와 같은 "[n] (제목, 위치)" 다 (가짜 AI 가 이 모양으로 번호를 읽는다).
    rewritten: 고쳐 쓴 질문. 원래 질문과 다를 때만 <고친 질문> 칸으로 넘긴다 ("세 번째 거" 같은 말을 대화에서 다시
    풀다가 다른 항목을 고르지 않게. 평가 P14). 답은 원래 질문에 한다."""
    parts = [conversation_block(turns)] if turns else []
    parts.append("<자료>")
    for i, e in enumerate(evidence, 1):
        parts.append(f"[{i}] ({_quote(e.title)}, {_quote((e.location or {}).get('label', ''))})\n{_quote(e.text)}")
    parts += ["</자료>", f"<질문>\n{_quote(query)}\n</질문>"]
    if rewritten and rewritten.strip() != query.strip():
        parts.append(f"<고친 질문>\n{_quote(rewritten)}\n</고친 질문>")
    return "\n\n".join(parts)


# ---------- ① 질문 고쳐 쓰기 ----------

@dataclass(frozen=True)
class Rewrite:
    query: str                   # 검색에 쓸 질문
    state: str                   # ok / skip / fallback_timeout / fallback_error / fallback_invalid
    ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None


def rewrite_query(llm, prompt: Prompt, turns, query, timeout_s) -> Rewrite:
    """대화가 없으면 부르지 않는다. 실패해도 예외를 내지 않고 원래 질문을 돌려준다."""
    if not turns:
        return Rewrite(query, "skip")
    t0 = time.monotonic()
    try:
        reply = llm.generate(prompt.text, rewrite_input(turns, query), timeout_s)
    except LLMTimeout:
        return Rewrite(query, "fallback_timeout", _ms(t0))
    except Exception:                                   # LLMError 와, 감싸지 못한 예외도 원래 질문으로
        return Rewrite(query, "fallback_error", _ms(t0))
    text = (reply.text or "").strip()
    usable = text and text != NO_ANSWER and "\n" not in text and len(text) <= REWRITE_MAX
    return Rewrite(text if usable else query, "ok" if usable else "fallback_invalid", _ms(t0),
                   reply.input_tokens, reply.output_tokens)


# ---------- 시간 ----------

@dataclass(frozen=True)
class Budget:
    """요청 하나의 시간 (명세 v0.2 4.4). 모두 정해 둔 값이며 측정 뒤 줄이는 쪽으로만 고친다."""
    deadline_s: float = 60.0         # 요청을 받은 때부터 응답까지
    rewrite_s: float = 5.0           # 고쳐 쓰기 (기본 모델만)
    primary_s: float = 25.0          # 답하기: 기본 모델
    fallback_min_s: float = 10.0     # 답하기: 남은 시간이 이보다 적으면 폴백을 부르지 않는다


def split_llm(llm):
    """(기본 모델, 폴백 모델 또는 None). 폴백은 서버 설정(RAG_LLM_FALLBACK_*)이 있을 때만 FallbackLLM 으로 감싸져 온다."""
    if isinstance(llm, FallbackLLM):
        return llm.primary, llm.fallback
    return llm, None


def generate_in_budget(llm, system, user, budget: Budget, left: Callable[[], float]):
    """기본 모델을 min(primary_s, 남은 시간) 동안 부르고, LLMError 면 남은 시간이 fallback_min_s 이상일 때만 폴백을 남은 시간 안에서.
    폴백이 없거나 시간이 모자라면 기본 모델의 예외를 그대로 낸다. 답한 모델은 Reply.model 에 담는다."""
    primary, fallback = split_llm(llm)
    timeout = min(budget.primary_s, left())
    if timeout <= 0:
        raise LLMTimeout("마감 시간이 지남")
    try:
        reply = primary.generate(system, user, timeout)
    except LLMError as e:
        remaining = left()
        if fallback is None or remaining < budget.fallback_min_s:
            raise
        log.warning("챗봇 기본 모델 실패, 폴백 모델로: primary=%s fallback=%s error=%s left_s=%.1f",
                    label(primary), label(fallback), e, remaining)
        return replace(fallback.generate(system, user, remaining), model=label(fallback), fallback=True)
    return replace(reply, model=label(primary)) if fallback else reply


# ---------- 전체 ----------

def _ms(since):
    return round((time.monotonic() - since) * 1000)


def _result(status, answer=None, citations=None, error_code=None, basis=None):
    if status == "no_result":
        answer = NOT_FOUND
    return {"status": status, "basis": basis, "answer": answer, "citations": citations or [], "errorCode": error_code}


@dataclass(frozen=True)
class ChatPrompts:
    chat: Prompt
    rewrite: Prompt


def chat(req: ChatRequest, connection, embedder, llm, prompts: ChatPrompts, budget: Budget = Budget(),
         request_id=None, clock=time.monotonic) -> dict:
    """명세의 응답 dict. DB 오류는 그대로 올린다 (API 가 503 으로 바꾼다). 마감을 넘겨도 200 (failed + llm_timeout)."""
    t0 = clock()

    def left():
        return budget.deadline_s - (clock() - t0)

    tag = (f"request_id={request_id} workspace={req.workspace_id} meeting={req.meeting_id} user={req.user_id} "
           f"history={len(req.history)} docs={len(req.docs)} live_agendas={len(req.live_agendas)} "
           f"recent_lines={len(req.recent_lines)}")
    if not req.docs:                                     # 지금 출처는 문서뿐이라 AI 를 부를 일이 없다
        log.info("챗봇 no_result(볼 문서 없음) %s", tag)
        return _result("no_result")

    turns = recent_turns(req.history)
    primary, _ = split_llm(llm)
    rw = rewrite_query(primary, prompts.rewrite, turns, req.query, min(budget.rewrite_s, left()))
    tag += f" rewrite={rw.state}"
    if rw.state != "skip":
        tag += f" rewrite_ms={rw.ms} rewrite_tokens_in={rw.input_tokens} rewrite_tokens_out={rw.output_tokens}"

    t1 = time.monotonic()
    evidence = doc_evidence(connection, embedder, req.workspace_id, req.docs, rw.query)
    tag += f" evidence={len(evidence)} search_ms={_ms(t1)}"
    if not evidence:
        log.info("챗봇 no_result(근거 없음) %s", tag)
        return _result("no_result")

    t2 = time.monotonic()
    tag += f" llm={llm.name}/{getattr(llm, 'model', '-')} prompt={prompts.chat.version}/{prompts.rewrite.version}"
    try:
        reply = generate_in_budget(llm, prompts.chat.text, chat_input(req.query, turns, evidence, rw.query),
                                   budget, left)
    except LLMTimeout as e:
        log.warning("챗봇 failed %s llm_ms=%d code=%s error=%s left_ms=%d", tag, _ms(t2), LLM_TIMEOUT, e,
                    left() * 1000)
        return _result("failed", error_code=LLM_TIMEOUT)
    except Exception as e:
        detail = e if isinstance(e, LLMError) else type(e).__name__
        log.warning("챗봇 failed %s llm_ms=%d code=%s error=%s left_ms=%d", tag, _ms(t2), LLM_FAILED, detail,
                    left() * 1000)
        return _result("failed", error_code=LLM_FAILED)
    tag += f" llm_ms={_ms(t2)} tokens_in={reply.input_tokens} tokens_out={reply.output_tokens}"
    if reply.model:
        tag += f" answered_by={reply.model}" + (" fallback=1" if reply.fallback else "")
    tag += f" left_ms={left() * 1000:.0f}"

    text = (reply.text or "").strip()
    status, refs, why = judge_reply(text, len(evidence))
    if why in ("no_answer", "no_refs"):
        log.info("챗봇 no_result(%s) %s total_ms=%d", why, tag, _ms(t0))
        return _result("no_result")
    if why == "bad_refs":
        bad = [n for n in refs if not 1 <= n <= len(evidence)]
        log.warning("챗봇 failed(범위 밖 번호 %d개) %s code=%s", len(bad), tag, LLM_FAILED)
        return _result("failed", error_code=LLM_FAILED)

    citations = [evidence[n - 1].citation(n) for n in sorted(refs)]
    log.info("챗봇 answered %s citations=%d total_ms=%d", tag, len(citations), _ms(t0))
    return _result("answered", answer=text, citations=citations, basis="sources")


def chat_prompts(chat_version: str | None = None, rewrite_version: str | None = None) -> ChatPrompts:
    """버전은 "v1" 또는 "chat-v1" (없으면 가장 큰 번호)."""
    cv = (chat_version or "").strip().removeprefix(f"{CHAT_KIND}-") or None
    rv = (rewrite_version or "").strip().removeprefix(f"{REWRITE_KIND}-") or None
    return ChatPrompts(load_prompt(CHAT_KIND, cv, (NO_ANSWER,)), load_prompt(REWRITE_KIND, rv))


class Chatter:
    """API 가 쓰는 챗봇 묶음 (answer.Answerer 와 같은 모양). 임베딩 모델을 불러오기 전에는 준비 안 됨(503)이다.
    고쳐 쓰기와 답하기는 같은 AI 설정(RAG_LLM_*)을 쓰고, 시간은 Budget 을 따른다."""

    def __init__(self, get_embedder: Callable, llm=None, budget: Budget = Budget(), config_error: str | None = None,
                 prompts: ChatPrompts | None = None, chat_version: str | None = None,
                 rewrite_version: str | None = None):
        self.get_embedder = get_embedder
        self.llm = llm
        self.budget = budget
        self.config_error = config_error
        self.prompts = prompts
        if prompts is None and config_error is None:
            try:
                self.prompts = chat_prompts(chat_version, rewrite_version)
            except PromptError as e:
                self.config_error = str(e)

    def not_ready_reason(self) -> str | None:
        if self.config_error:
            return f"챗봇 설정 오류: {self.config_error}"
        if self.get_embedder() is None:
            return "임베딩 모델을 아직 불러오지 않음"
        return None

    def run(self, connection, req: ChatRequest, request_id=None) -> dict:
        return chat(req, connection, self.get_embedder(), self.llm, self.prompts, self.budget, request_id)
