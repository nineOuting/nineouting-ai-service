"""안건 요약 받아쓰기 교정 채점 도구 (모델을 부르지 않는다, 비용 0원).

입력: 정답표(qN_corrections_truth.json, confirmed:true 여야 함), eval_agenda_summary.py 의 --items-out 파일.
기계로 세는 것: E1 틀린 정정, E2 숫자·날짜·이름 정정, E4 몰래 고침, E5 정정 재현율, E6 이상한 원문 잔존, E7 애매한 말을 고침.
사람이 보는 것: E3 지어낸 문장, 읽기 자연스러움 -> --sheet-out 채점표.
기준은 평가 전에 정한 값을 아래 CRITERIA 에 고정했다. 결과를 본 뒤 바꾸지 않는다.
전사·문장이 들어 있는 파일(items, sheet)은 저장소에 올리지 않는다.

  python3 eval_corrections.py --truth T.json --items I.json [--mode gold] [--transcript Q.jsonl --sheet-out S.tsv] [--out R.json]
  python3 eval_corrections.py --selftest
"""
import argparse
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from meeting_summary import squash, load_jsonl_as_protocol_lines  # noqa: E402

CRITERIA = {"E1": 0, "E2": 0, "E3": 0, "E4": 0, "E5_min": 0.70, "E6_max": 2, "E7": 0, "E9_min": 0.70}
PROTECTED = ("number", "date", "numberlike", "name")      # 정정하면 안 되는 종류 (E2)


# v5 의 표시. 풀리지 않는 말을 「」 로 인용하고 바로 뒤에 붙인 것만 '표시한 말'로 본다(다른 모양의 STT 불확실 안내는 표시로 치지 않는다).
MARK_RE = re.compile(r"「[^」]*」\s*\(\s*받아쓰기\s*불확실\s*\)")


def unmarked(text):
    return MARK_RE.sub("", text)


def norm(s):
    return squash(s).lower()


def fix_ok(e, c):
    """정정 c 가 정답표 오류 e 를 제대로 고친 것인가. from 이 받아쓴 말보다 길 때(앞뒤 낱말 포함)는 to 에 정답이 들어 있으면 되고,
    from 이 받아쓴 말의 일부일 때(예: 벡터 디비 중 디비만)는 그 부분을 to 로 바꾸면 정답이 되어야 한다."""
    heard, corr, f, t = norm(e["heard"]), norm(e["correct"] or ""), norm(c["from"]), norm(c["to"])
    if heard in f:
        return corr in t
    return f in heard and heard.replace(f, t, 1) == corr


def same_error(e, c):
    """정정 c 가 정답표 오류 e 에 대한 것인가 (from 과 받아쓴 말이 서로 포함 관계)."""
    heard, f = norm(e["heard"]), norm(c["from"])
    return heard in f or (len(f) >= 2 and f in heard)


def load_truth(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if d.get("confirmed") is not True:
        sys.exit("정답표가 확정되지 않았습니다 (confirmed:true 필요)")
    return d["errors"]


def load_items(path, mode):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if mode not in d["modes"]:
        sys.exit(f"--mode {mode} 가 items 파일에 없습니다: {list(d['modes'])}")
    items = []
    for ag, rows in d["modes"][mode]["agendas"].items():
        for x in rows:
            items.append({**x, "agenda": ag})
    return d.get("promptVersion"), items


def score(errors, items):
    by_seq = {}
    for e in errors:
        by_seq.setdefault(e["seq"], []).append(e)
    alltext = [norm(x["text"]) for x in items]
    plain = [norm(unmarked(x["text"])) for x in items]            # 표시한 말을 뺀 본문
    r = {"E1": [], "E2": [], "E4": [], "E6": [], "E7": [], "unplanted": [], "marked": [],
         "marks": sum(len(MARK_RE.findall(x["text"])) for x in items)}
    # 정정 목록 대조 (E1, E2, E7)
    for x in items:
        for c in x.get("corrections", []):
            cand = [e for e in by_seq.get(c.get("seq"), []) if same_error(e, c)]   # from 이 받아쓴 말보다 길거나 일부여도 같은 정정으로 본다
            if not cand:
                r["unplanted"].append({"item": x["id"], "from": c["from"], "to": c["to"], "seq": c.get("seq")})
                continue
            e = cand[0]
            if e["type"] in PROTECTED:
                r["E2"].append({"item": x["id"], "error": e["id"]})
            elif e["type"] == "ambiguous":
                r["E7"].append({"item": x["id"], "error": e["id"]})
                r["E1"].append({"item": x["id"], "error": e["id"], "why": "애매한 말을 고침"})
            elif not e["fix"]:
                r["E1"].append({"item": x["id"], "error": e["id"], "why": "고치면 안 되는 말"})
            elif not fix_ok(e, c):
                r["E1"].append({"item": x["id"], "error": e["id"], "why": "고친 말이 틀림"})
    # 오류별 (E4, E5, E6)
    used = ok = 0
    for e in errors:
        cites = [x for x in items if e["seq"] in x["evidenceSeqs"]]
        heard, corr = norm(e["heard"]), (norm(e["correct"]) if e["correct"] else None)
        if e["type"] != "control" and e["type"] not in ("number", "date", "numberlike") and any(heard in t for t in plain):
            r["E6"].append(e["id"])          # 표시 없이 남은 이상한 원문
        elif e["type"] in ("ambiguous", "name") and any(heard in t for t in alltext):
            r["marked"].append(e["id"])      # 표시를 붙여 남긴 말 (E6 에 세지 않음, 참고)
        if not e["fix"]:
            continue
        has_corr = lambda x: any(c.get("seq") == e["seq"] and same_error(e, c) and fix_ok(e, c)
                                 for c in x.get("corrections", []))
        any_corr = lambda x: any(c.get("seq") == e["seq"] and same_error(e, c) for c in x.get("corrections", []))
        touched = [x for x in cites if heard in norm(x["text"]) or corr in norm(x["text"]) or any_corr(x)]
        if touched:
            used += 1
            if any(corr in norm(x["text"]) for x in touched) and not any(heard in norm(x["text"]) for x in cites):
                ok += 1
        for x in cites:
            if corr in norm(x["text"]) and heard not in norm(x["text"]) and not has_corr(x):
                r["E4"].append({"item": x["id"], "error": e["id"]})
    r["used"], r["recovered"] = used, ok
    r["E5"] = (ok / used) if used else None
    return r


def judge(r):
    c = CRITERIA
    e1 = len(r["E1"])
    return {"E1": e1 == c["E1"] and (not r["unplanted"] or "확인 필요"),
            "E2": len(r["E2"]) == c["E2"], "E4": len(r["E4"]) == c["E4"],
            "E5": r["E5"] is not None and r["E5"] >= c["E5_min"],
            "E6": len(r["E6"]) <= c["E6_max"], "E7": len(r["E7"]) == c["E7"],
            "E3": "사람 채점", "읽기": "사람 채점"}


def e9_points(errors, keypoints, items):
    """풀리지 않는 말(ambiguous)이 걸린 줄을 근거로 하는 핵심 내용과, 근거가 겹치는 요약 문장. 겹침은 상한일 뿐이고 실제로 살아 있는지는 사람이 본다."""
    amb = {e["seq"] for e in errors if e["type"] == "ambiguous"}
    out = []
    for ag, ks in keypoints.items():
        for k in ks:
            if amb & set(k["evidenceSeqs"]):
                cov = [x for x in items if x["agenda"] == ag and set(k["evidenceSeqs"]) & set(x["evidenceSeqs"])]
                out.append({"agenda": ag, "id": k["id"], "text": k["text"], "covering": [x["id"] for x in cov],
                            "covering_text": " / ".join(x["text"] for x in cov)})
    return out


def write_sheet(path, items, transcript, points=()):
    store = {ln["id"]: ln["text"] for ln in load_jsonl_as_protocol_lines(transcript)} if transcript else {}
    with open(path, "w", encoding="utf-8") as f:
        f.write("agenda\tid\ttext\tevidence\tevidence_text\t정정 목록\t지어냄 E3(O/X)\t읽기 자연스러움(1-5)\t메모\n")
        for x in items:
            ev = " / ".join(f"[{s}] {store.get(s, '')}" for s in x["evidenceSeqs"])
            co = "; ".join(f"{c['from']}->{c['to']}@{c.get('seq')}" for c in x.get("corrections", []))
            f.write(f"{x['agenda']}\t{x['id']}\t{x['text']}\t{','.join(map(str, x['evidenceSeqs']))}\t{ev}\t{co}\t\t\t\n")
        if points:
            f.write("\n핵심(모호한 말 관련)\tagenda\t핵심 내용(정답)\t겹친 문장\t겹친 문장 본문\t\t살아 있음 E9(O/X)\t\t메모\n")
            for p in points:
                f.write(f"핵심\t{p['agenda']}\t{p['text']}\t{','.join(p['covering']) or '없음'}\t{p['covering_text']}\t\t\t\t\n")


def selftest():
    E = lambda i, t, fix, c, seq, h: {"id": i, "type": t, "fix": fix, "correct": c, "seq": seq, "heard": h}
    errs = [E("a", "acronym", True, "RAG", 1, "레그"), E("b", "term", True, "임베딩", 2, "인베딩"),
            E("c", "ambiguous", False, None, 3, "바라솔"), E("d", "number", False, None, 4, "열 개"),
            E("e", "control", False, None, 5, "스트리밍"), E("f", "name", False, None, 6, "서면")]
    I = lambda i, t, ev, co=(): {"id": i, "agenda": "a1", "text": t, "evidenceSeqs": ev, "corrections": list(co)}
    C = lambda f, t, s: {"from": f, "to": t, "seq": s}
    ok = score(errs, [I("1", "RAG 단계 문제", [1], [C("레그", "RAG", 1)]), I("2", "임베딩 교체", [2], [C("인베딩", "임베딩", 2)])])
    assert ok["E5"] == 1.0 and not ok["E1"] and not ok["E4"] and not ok["E6"] and not ok["E2"] and not ok["E7"]
    # E1: 고친 말이 틀림, 고치면 안 되는 말(control)
    r = score(errs, [I("1", "RGA", [1], [C("레그", "RGA", 1)]), I("2", "스트리밍", [5], [C("스트리밍", "스트림", 5)])])
    assert len(r["E1"]) == 2 and r["E5"] == 0.0
    # E2 숫자·이름, E7 애매한 말
    r = score(errs, [I("1", "x", [4], [C("열 개", "10개", 4)]), I("2", "y", [6], [C("서면", "서연", 6)]), I("3", "z", [3], [C("바라솔", "바라솔라", 3)])])
    assert len(r["E2"]) == 2 and len(r["E7"]) == 1 and len(r["E1"]) == 1
    # 계획에 없는 정정은 따로 센다
    r = score(errs, [I("1", "w", [1], [C("엉뚱", "정정", 1)])])
    assert len(r["unplanted"]) == 1 and judge(r)["E1"] == "확인 필요"
    # E4 몰래 고침: 정정 목록 없이 고친 말만 나옴
    r = score(errs, [I("1", "RAG 단계", [1])])
    assert len(r["E4"]) == 1 and r["used"] == 1 and r["E5"] == 1.0
    # E5: 그대로 둔 오류는 재현 실패, 오류와 무관하게 빠진 오류는 분모에서 제외
    r = score(errs, [I("1", "레그 단계", [1]), I("2", "다른 이야기", [9])])
    assert r["used"] == 1 and r["E5"] == 0.0 and r["E6"] == ["a"]
    r = score(errs, [I("1", "다른 이야기", [9])])
    assert r["used"] == 0 and r["E5"] is None and judge(r)["E5"] is False
    # E6: 이상한 원문 잔존은 이름·애매한 말도 센다, 숫자·통제 단어는 세지 않는다
    r = score(errs, [I("1", "바라솔 서면 열 개 스트리밍", [3, 4, 5, 6])])
    assert sorted(r["E6"]) == ["c", "f"]
    # 한 오류가 여러 문장에 걸려도 한 번만 센다, 띄어쓰기·대소문자 차이는 같은 말로 본다
    r = score(errs, [I("1", "레 그", [1]), I("2", "레그", [1])])
    assert r["E6"] == ["a"]
    r = score(errs, [I("1", "r a g 단계", [1], [C("레 그", "rag", 1)])])
    assert r["E5"] == 1.0 and not r["E1"]
    # 숫자처럼 보이는 말: 정정하면 E2, 남아 있어도 E6 로 세지 않는다
    en = errs + [E("g", "numberlike", False, None, 7, "일번 안")]
    r = score(en, [I("1", "일번 안", [7], [C("일번 안", "1안", 7)])])
    assert len(r["E2"]) == 1 and r["E6"] == []
    r = score(en, [I("1", "열 개", [4])])
    assert r["E6"] == []
    # 같은 오류를 인용한 다른 문장에 원문이 남아 있으면 재현 실패
    r = score(errs, [I("1", "RAG", [1], [C("레그", "RAG", 1)]), I("2", "레그 문제", [1])])
    assert r["E5"] == 0.0
    # E4: 원문과 고친 말이 같이 있으면 몰래 고친 게 아니다. 틀린 정정이 목록에 있어도 목록 없는 것과 같다. 다른 줄을 인용한 문장은 보지 않는다
    r = score(errs, [I("1", "레그 RAG", [1])])
    assert r["E4"] == []
    r = score(errs, [I("1", "RAG", [1], [C("레그", "RGA", 1)])])
    assert len(r["E4"]) == 1
    r = score(errs, [I("1", "RAG", [9])])
    assert r["E4"] == [] and r["used"] == 0
    r = score(errs, [I("1", "RAG", [1], [C("레그", "RAG", 2)])])
    assert len(r["E4"]) == 1
    # judge 는 E2, E4, E7 도 본다
    base = {"E1": [], "E2": [], "E4": [], "E6": [], "E7": [], "unplanted": [], "E5": 1.0}
    assert judge({**base, "E2": [1]})["E2"] is False and judge({**base, "E4": [1]})["E4"] is False
    assert judge({**base, "E7": [1]})["E7"] is False and judge(base)["E2"] is True
    # from·to 에 앞뒤 낱말이 붙어도 같은 정정이다 (계획에 없는 정정으로 세지 않고, 몰래 고침으로도 세지 않는다)
    r = score(errs, [I("1", "RAG 제거", [1], [C("레그 제거", "RAG 제거", 1)])])
    assert r["unplanted"] == [] and r["E4"] == [] and r["E1"] == [] and r["E5"] == 1.0
    r = score(errs, [I("1", "RGA 제거", [1], [C("레그 제거", "RGA 제거", 1)])])
    assert len(r["E1"]) == 1 and r["unplanted"] == []
    # v5 표시: 「」 인용 + (받아쓰기 불확실) 은 E6 에 세지 않고 따로 센다. 표시가 없거나 다른 모양이면 E6 에 센다
    r = score(errs, [I("1", "「바라솔」(받아쓰기 불확실) 방식은 뺀다", [3]), I("2", "「서면」 (받아쓰기  불확실) 님 요청", [6])])
    assert r["E6"] == [] and sorted(r["marked"]) == ["c", "f"] and r["marks"] == 2
    r = score(errs, [I("1", "「바라솔」 방식은 뺀다 (STT 불확실)", [3])])
    assert r["E6"] == ["c"] and r["marked"] == [] and r["marks"] == 0
    r = score(errs, [I("1", "「바라솔」(STT 불확실) 방식", [3])])
    assert r["E6"] == ["c"] and r["marks"] == 0, "다른 모양의 안내는 표시로 치지 않는다"
    r = score(errs, [I("1", "「레그」(받아쓰기 불확실) 단계", [1])])
    assert r["marked"] == [], "고칠 수 있는 말을 표시한 것은 표시한 말 목록에 넣지 않는다"
    r = score(errs, [I("1", "바라솔(받아쓰기 불확실) 방식", [3])])
    assert r["E6"] == ["c"], "「」 없이 표시만 붙이면 표시로 치지 않는다"
    r = score(errs, [I("1", "「레그」(받아쓰기 불확실) 단계", [1])])
    assert r["E6"] == [] and r["E5"] == 0.0, "고칠 수 있는 말을 표시로 남기면 E5 실패다(E6 에는 세지 않는다)"
    assert unmarked("가 「나」(받아쓰기 불확실) 다") == "가  다"
    # from 이 받아쓴 말의 일부여도(벡터 디비 중 디비) 바르게 고쳤으면 같은 정정이다. 틀리게 고쳤으면 E1
    ep = [E("p", "term", True, "벡터 DB", 3, "벡터 디비")]
    r = score(ep, [I("1", "벡터 DB 설정", [3], [C("디비", "DB", 3)])])
    assert r["unplanted"] == [] and r["E4"] == [] and r["E1"] == [] and r["E5"] == 1.0, r
    r = score(ep, [I("1", "벡터 DBMS 설정", [3], [C("디비", "DBMS", 3)])])
    assert len(r["E1"]) == 1 and r["unplanted"] == []
    r = score(ep, [I("1", "벡터 DB 설정", [3], [C("디", "D", 3)])])
    assert len(r["unplanted"]) == 1 and len(r["E4"]) == 1, "한 글자 from 은 같은 정정으로 보지 않는다"
    assert fix_ok(ep[0], C("디비", "DB", 3)) and not fix_ok(ep[0], C("디비", "DBMS", 3)) and fix_ok(ep[0], C("벡터 디비", "벡터 DB", 3))
    # 기준값 고정
    assert CRITERIA == {"E1": 0, "E2": 0, "E3": 0, "E4": 0, "E5_min": 0.70, "E6_max": 2, "E7": 0, "E9_min": 0.70}
    # E9: 풀리지 않는 말(ambiguous)이 걸린 핵심 내용만 고르고, 근거가 겹치는 같은 안건의 문장을 붙인다
    ee = errs + [E("h", "ambiguous", False, None, 9, "세르비나")]
    kp = {"a1": [{"id": "k1", "text": "t1", "evidenceSeqs": [9]}, {"id": "k2", "text": "t2", "evidenceSeqs": [1]}],
          "a2": [{"id": "k1", "text": "t3", "evidenceSeqs": [9, 10]}]}
    its = [I("1", "문장1", [9, 10]), {**I("2", "문장2", [9]), "agenda": "a2"}]
    pts = e9_points(ee, kp, its)
    assert [(p["agenda"], p["id"], p["covering"]) for p in pts] == [("a1", "k1", ["1"]), ("a2", "k1", ["2"])], pts
    assert e9_points(ee, kp, [I("3", "x", [5])])[0]["covering"] == [] and e9_points(errs, kp, its) == []
    kp_name = {"a1": [{"id": "k9", "text": "t9", "evidenceSeqs": [6]}]}
    assert e9_points(ee, kp_name, its) == [], "이름 오류(name)가 걸린 핵심 내용은 E9 대상이 아니다"
    # 명령줄: 겹침 개수가 JSON 에 맞게 나온다
    import subprocess
    with tempfile.TemporaryDirectory() as d3:
        tj, ij, kj = (os.path.join(d3, n) for n in ("t.json", "i.json", "k.json"))
        json.dump({"confirmed": True, "errors": ee}, open(tj, "w"))
        json.dump({"promptVersion": "v3", "modes": {"gold": {"agendas": {"a1": [I("1", "문장1", [9, 10])], "a2": [I("2", "문장2", [20])]}}}}, open(ij, "w"))
        json.dump({"agendas": kp}, open(kj, "w"))
        out = subprocess.run([sys.executable, os.path.abspath(__file__), "--truth", tj, "--items", ij, "--keypoints", kj], capture_output=True, text=True)
        res = json.loads(out.stdout)
        assert res["E9_points"] == 2 and res["E9_overlap"] == 1, res
    with tempfile.TemporaryDirectory() as d2:
        sp = os.path.join(d2, "s.tsv")
        write_sheet(sp, its, None, pts)
        assert "핵심(모호한 말 관련)" in open(sp, encoding="utf-8").read()
    j = judge({"E1": [], "E2": [], "E4": [], "E6": ["x", "y"], "E7": [], "unplanted": [], "E5": 0.70})
    assert j["E5"] is True and j["E6"] is True
    j = judge({"E1": [], "E2": [], "E4": [], "E6": ["x", "y", "z"], "E7": [], "unplanted": [], "E5": 0.69})
    assert j["E5"] is False and j["E6"] is False
    # 파일 입출력
    with tempfile.TemporaryDirectory() as d:
        t, it = os.path.join(d, "t.json"), os.path.join(d, "i.json")
        json.dump({"confirmed": True, "errors": errs}, open(t, "w"))
        json.dump({"promptVersion": "v2", "modes": {"gold": {"agendas": {"a1": [I("1", "RAG", [1])]}}}}, open(it, "w"))
        assert len(load_truth(t)) == 6
        pv, items = load_items(it, "gold")
        assert pv == "v2" and items[0]["agenda"] == "a1"
        sh = os.path.join(d, "s.tsv")
        write_sheet(sh, items, None)
        assert open(sh, encoding="utf-8").read().count("\n") == 2
        json.dump({"confirmed": False, "errors": errs}, open(t, "w"))
        try:
            load_truth(t)
            assert False
        except SystemExit:
            pass
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth")
    ap.add_argument("--items")
    ap.add_argument("--mode", default="gold")
    ap.add_argument("--transcript")
    ap.add_argument("--keypoints", help="핵심 내용 JSON. 주면 모호한 말이 걸린 핵심 내용(E9)을 센다")
    ap.add_argument("--sheet-out")
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not (a.truth and a.items):
        sys.exit("--truth 와 --items 가 필요합니다")
    for p in (a.sheet_out, a.out):
        if p and os.path.exists(p):
            sys.exit(f"{p} 가 이미 있습니다")
    errors = load_truth(a.truth)
    pv, items = load_items(a.items, a.mode)
    r = score(errors, items)
    points = []
    if a.keypoints:
        with open(a.keypoints, encoding="utf-8") as f:
            points = e9_points(errors, json.load(f)["agendas"], items)
    res = {"promptVersion": pv, "mode": a.mode, "criteria": CRITERIA, "sentences": len(items),
           "E1": len(r["E1"]), "E2": len(r["E2"]), "E4": len(r["E4"]), "E5": r["E5"], "E5_used": r["used"],
           "E5_recovered": r["recovered"], "E6": len(r["E6"]), "E7": len(r["E7"]), "unplanted": len(r["unplanted"]),
           "marked_kept": len(r["marked"]), "marks_per_sentence": round(r["marks"] / len(items), 2) if items else None,
           "E9_points": len(points), "E9_overlap": sum(1 for p in points if p["covering"]),
           "E9_note": "겹침은 상한이다. 살아 있는지는 채점표의 E9 칸을 사람이 채워 센다(합격선 E9_min)",
           "pass": judge(r), "detail": {k: r[k] for k in ("E1", "E2", "E4", "E6", "E7", "unplanted")}}
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    if a.sheet_out:
        write_sheet(a.sheet_out, items, a.transcript, points)
    print(text)


if __name__ == "__main__":
    main()
