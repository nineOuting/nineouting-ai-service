#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
미결 안건 추출 (AI-07)

회의를 끝내기 전에 결론이 나지 않은 안건 목록과 지금까지의 논의 요약(AI-03 요약 문장)을 안건 보드의 leftover 칸에 담는다.
형식은 docs/api/agenda-leftover-contract.md, 예시는 examples/api/. 모델은 부르지 않는다 (비용 0).

두 가지 시점
  종료 버튼 (end-requests, action "request"): 목록만 계산해 보여 준다. 안건 상태는 확정하지 않는다.
      안건 상태를 종료 확정으로 가정한 값(AgendaStatusJudge.board_status(preview_final=True))으로 목록을 만든다.
      이어서 논의하기(action "cancel")를 받으면 목록을 거두고 회의는 그대로 이어진다.
      요청을 받으면 한 번 대기 규칙을 건너뛰고 분류, 상태 판정, 안건 요약을 돌린다(flush). 방금 나온 결정이 목록에 남지 않게 하려는 것이다.
  회의 끝 (end 알림): 목록이 마지막 값이 된다 (final: true). 요청이 없었어도 마지막 보드에는 목록이 들어간다.

넘길 후보: 상태가 UNDECIDED, NOT_STARTED, DECISION_CHECK 인 안건 (종료 확정을 가정한 값 기준).
  빈 목록은 남은 안건이 없다는 뜻이다. 칸 자체가 null 이면 요청이 없거나 취소되었고 회의도 끝나지 않았다는 뜻이다.

요청 번호(rev): 큰 값만 반영한다 (무시 알림과 같은 규칙). 시작값은 상관없다.
스레드: 요청 스레드(request, info)와 일꾼 스레드(take_flush, process, state)가 함께 쓴다. 락 하나로 지킨다.
로그 규칙: 전사 내용, 안건 제목, 요약 문장을 남기지 않는다. 번호와 개수만 남긴다.
"""
import copy
import logging
import threading

log = logging.getLogger("ai-service.agenda-leftover")

LEFTOVER_STATUSES = ("UNDECIDED", "NOT_STARTED", "DECISION_CHECK")   # 다음 회의로 넘길 후보 상태
ACTIONS = ("request", "cancel")


class AgendaLeftover:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_rev = None          # 반영한 가장 큰 요청 번호 (취소 포함)
        self.req_rev = None           # 마지막으로 반영한 종료 버튼 요청(request)의 번호
        self.active = False           # 종료 버튼이 눌린 상태 (취소하면 꺼진다)
        self.final = False            # 회의가 끝났다
        self.flush_pending = False    # 요청을 새로 받아 한 번 대기를 건너뛰고 돌려야 한다
        self.requests = 0             # 반영한 종료 버튼 요청 수

    # ---------------- 사람의 응답 ----------------
    def request(self, action, rev):
        """종료 버튼(request) 또는 이어서 논의하기(cancel).
        반환: "applied" / "stale"(번호가 오래됨, 끝난 회의 포함) / "not_active"(떠 있는 요청이 없는데 cancel. 번호는 기록한다)."""
        with self.lock:
            if self.final:
                return "stale"                                   # 끝난 회의는 서비스가 먼저 거른다. 겹쳐 도착한 경우의 안전장치
            if self.last_rev is not None and rev <= self.last_rev:
                return "stale"
            self.last_rev = rev
            if action == "request":
                self.active, self.flush_pending = True, True
                self.req_rev = rev
                self.requests += 1
                log.info("종료 버튼 요청 반영 rev=%s", rev)
                return "applied"
            was_active = self.active
            self.active, self.flush_pending = False, False
            log.info("이어서 논의하기 반영 rev=%s active=%s", rev, was_active)
            return "applied" if was_active else "not_active"

    # ---------------- 일꾼 ----------------
    def take_flush(self):
        """요청을 새로 받았으면 True를 한 번만 돌려준다. 이때 분류, 상태 판정, 안건 요약의 대기 규칙을 건너뛴다."""
        with self.lock:
            flush = self.flush_pending and self.active and not self.final
            self.flush_pending = False
            return flush

    def process(self, final=False):
        """회의가 끝나면 목록을 마지막 값으로 굳힌다."""
        if final:
            with self.lock:
                self.final = True
                self.flush_pending = False

    def state(self):
        """보드 leftover 칸의 머리값 {"requestRev", "final"}. 칸이 없어야 하면 None."""
        with self.lock:
            if not (self.active or self.final):
                return None
            return {"requestRev": self.req_rev, "final": self.final}

    def info(self):
        """상태 조회용. 안건 제목, 요약 문장은 넣지 않는다."""
        with self.lock:
            return {"requestRev": self.req_rev, "active": self.active, "final": self.final, "requests": self.requests}


def build_leftover(meta, ents):
    """보드의 leftover 칸을 만든다.
    meta: {"requestRev", "final", "status_info"}  status_info 는 안건 key -> {"status", "reason"} (종료 확정을 가정한 값)
    ents: [(안건 key, 보드의 안건 항목)] 보드의 agendas[] 순서
    넘길 후보가 아닌 안건은 뺀다. 요약 문장은 복사해 담는다(이후 보드가 바뀌어도 이 값은 그대로)."""
    info = meta["status_info"]
    out = []
    for key, ent in ents:
        si = info.get(key)
        if si is None or si["status"] not in LEFTOVER_STATUSES:
            continue
        r = si.get("reason")
        out.append({"agendaId": ent["agendaId"], "status": si["status"],
                    "statusReason": {"text": r["text"], "evidenceSeqs": list(r["evidenceSeqs"])} if r else None,
                    "summary": copy.deepcopy(ent["summary"])})
    return {"requestRev": meta["requestRev"], "final": bool(meta["final"]), "agendas": out}


# --------------------------------------------------------------------------------------
def selftest():
    # 1) 요청 규칙: 큰 번호만 반영, 취소, 떠 있는 요청이 없는 취소는 번호만 기록
    a = AgendaLeftover()
    assert a.state() is None and a.take_flush() is False
    assert a.request("request", 5) == "applied" and a.state() == {"requestRev": 5, "final": False}
    assert a.request("request", 5) == "stale" and a.request("cancel", 4) == "stale"
    assert a.take_flush() is True and a.take_flush() is False, "요청마다 flush는 한 번"
    assert a.request("cancel", 6) == "applied" and a.state() is None and a.take_flush() is False
    assert a.request("cancel", 7) == "not_active" and a.request("request", 7) == "stale", "취소는 번호를 기록한다"
    assert a.request("request", 8) == "applied" and a.request("request", 9) == "applied" and a.take_flush() is True
    assert a.info() == {"requestRev": 9, "active": True, "final": False, "requests": 3}

    # 2) 순서가 바뀌어 도착해도 정리된다 (재시작 복구): 취소(6)가 먼저, 오래된 요청(5)이 나중에
    a = AgendaLeftover()
    assert a.request("cancel", 6) == "not_active" and a.request("request", 5) == "stale" and a.state() is None
    assert a.request("request", 3 + 4) == "applied"

    # 3) 끝: 요청이 없었어도 칸이 생긴다. requestRev 는 None. 취소한 뒤 끝나도 칸이 생긴다. 끝난 뒤 요청은 받지 않는다
    a = AgendaLeftover()
    a.process(final=False)
    assert a.state() is None
    a.process(final=True)
    assert a.state() == {"requestRev": None, "final": True} and a.request("request", 1) == "stale" and a.take_flush() is False
    a = AgendaLeftover()
    a.request("request", 2)
    a.request("cancel", 3)
    a.process(final=True)
    assert a.state() == {"requestRev": 2, "final": True} and a.take_flush() is False
    a = AgendaLeftover()
    a.request("request", 2)
    a.process(final=True)
    assert a.take_flush() is False, "끝났으면 flush 없이 마지막 판정으로 간다"

    # 4) 목록 만들기: 후보 상태만, 보드 순서대로, 요약 문장은 복사
    def ent(aid, texts=()):
        return {"agendaId": aid, "summary": [{"id": f"s{aid}-{i}", "text": t, "evidenceSeqs": [i + 1], "edited": i == 0, "needsCheck": False}
                                             for i, t in enumerate(texts)]}
    why = lambda t, *s: {"text": t, "evidenceSeqs": list(s)}
    status = {"k1": {"status": "DECIDED", "reason": why("결정", 3)},
              "k2": {"status": "UNDECIDED", "reason": why("미결", 9)},
              "k3": {"status": "NOT_STARTED", "reason": None},
              "k4": {"status": "DECISION_CHECK", "reason": why("확인", 12, 13)},
              "k5": {"status": "DISCUSSING", "reason": why("논의 중", 20)}}      # 미리보기에서는 오지 않지만 오면 거른다
    ents = [("k1", ent(101, ["a"])), ("k2", ent(102, ["b", "c"])), ("k3", ent(103)), ("k4", ent(104, ["d"])), ("k5", ent(105, ["e"])),
            ("k6", ent(106, ["f"]))]                                              # k6 은 상태가 없다 -> 거른다
    out = build_leftover({"requestRev": 4, "final": False, "status_info": status}, ents)
    assert out["requestRev"] == 4 and out["final"] is False
    assert [x["agendaId"] for x in out["agendas"]] == [102, 103, 104], out
    assert [x["status"] for x in out["agendas"]] == ["UNDECIDED", "NOT_STARTED", "DECISION_CHECK"]
    assert out["agendas"][1]["statusReason"] is None and out["agendas"][2]["statusReason"] == why("확인", 12, 13)
    assert [s["text"] for s in out["agendas"][0]["summary"]] == ["b", "c"] and out["agendas"][1]["summary"] == []
    assert set(out["agendas"][0]) == {"agendaId", "status", "statusReason", "summary"}
    assert set(out["agendas"][0]["summary"][0]) == {"id", "text", "evidenceSeqs", "edited", "needsCheck"}, "요약 문장은 보드 모양 그대로"
    ents[1][1]["summary"][0]["text"] = "바뀜"
    status["k4"]["reason"]["evidenceSeqs"].append(99)
    assert out["agendas"][0]["summary"][0]["text"] == "b" and out["agendas"][2]["statusReason"]["evidenceSeqs"] == [12, 13], "복사해 담는다"
    assert build_leftover({"requestRev": None, "final": True, "status_info": {"k1": status["k1"]}}, ents) == {
        "requestRev": None, "final": True, "agendas": []}, "남은 안건이 없으면 빈 목록"
    print("AI-07 미결 안건 추출 자체 시험 통과 (요청 번호 규칙, 취소, 순서 뒤바뀜, 끝, flush 한 번, 후보 거르기, 복사)")


if __name__ == "__main__":
    selftest()
