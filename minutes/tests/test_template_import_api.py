"""양식 가져오기 주소 시험 (SCRUM-289). 모델·DB·네트워크 없이 돈다: 가짜 AI 와 가짜 파일 받기를 쓴다."""
import logging
import threading

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI                     # noqa: E402
from fastapi.testclient import TestClient      # noqa: E402

from minutes import import_api as api          # noqa: E402
from minutes import template_import as ti      # noqa: E402
from minutes.placement import CHECK_FAILED, LLM_FAILED, load_prompt    # noqa: E402
from minutes_fakes import ScriptLLM            # noqa: E402
from rag.download import UploadSettings        # noqa: E402
from rag.errors import DOWNLOAD_FAILED, INTERNAL_ERROR, IngestError, TOO_LARGE, UNSUPPORTED_FORMAT, URL_EXPIRED   # noqa: E402
from shared.llm import LLMError                # noqa: E402
from test_template_import import PAGES, good, pdf   # noqa: E402

URL = "/internal/minutes/template-imports"
HOST = "files.example.com"
SIGNED = f"https://{HOST}/templates/form.pdf?X-Amz-Signature=SECRET-SIG"
UPLOAD = UploadSettings(allowed_hosts=frozenset({HOST}), max_bytes=1024 * 1024, timeout_s=5.0)


@pytest.fixture
def lines():
    return ti.number_lines(PAGES)


def app_with(llm, upload=UPLOAD, config_error=None, max_concurrent=4):
    app = FastAPI()
    app.include_router(api.make_import_router(llm, 30.0, upload, load_prompt(None, ti.KIND), config_error, max_concurrent))
    return TestClient(app)


@pytest.fixture
def fetched(monkeypatch):
    """파일 받기를 가짜로 바꾼다. 받은 주소를 기록하고, 정해 둔 파일(또는 예외)을 돌려준다."""
    state = {"urls": [], "data": pdf("upload_report.pdf")}

    def fake_fetch(url, settings):
        state["urls"].append(url)
        if isinstance(state["data"], Exception):
            raise state["data"]
        return state["data"]
    monkeypatch.setattr(api, "fetch", fake_fetch)
    return state


def report_answer():
    ls = ti.read_lines(pdf("upload_report.pdf"))
    r = next(x.ref for x in ls if x.text == "1. 개요")
    h = next(x.ref for x in ls if x.text.startswith("나인아웃팅 합성 자료"))
    return {"header": [h], "footer": [], "fields": [{"ref": r, "title": "개요", "type": "free", "columns": None, "descriptionRefs": []}]}


# ---- 성공

def test_파일을_받아_양식_항목을_돌려준다(fetched):
    llm = ScriptLLM(report_answer())
    r = app_with(llm).post(URL, json={"requestId": "r-1", "fileUrl": SIGNED})
    assert r.status_code == 200
    assert r.json() == {"requestId": "r-1", "status": "SUCCEEDED", "fields": [{"title": "개요", "type": "free", "aiFill": True}],
                        "header": "나인아웃팅 합성 자료 · 내부 검토용", "footer": None, "warnings": []}
    assert fetched["urls"] == [SIGNED] and len(llm.calls) == 1


def test_requestId가_없어도_된다_답에도_안_넣는다(fetched):
    r = app_with(ScriptLLM(report_answer())).post(URL, json={"fileUrl": SIGNED})
    assert r.status_code == 200 and r.json()["status"] == "SUCCEEDED" and "requestId" not in r.json()


def test_경고도_함께_돌려준다(fetched):
    ls = ti.read_lines(pdf("upload_report.pdf"))
    ref = next(x.ref for x in ls if x.text == "1. 개요")
    ans = {"header": [], "footer": [], "fields": [{"ref": ref, "title": "개요", "type": "checklist", "columns": None, "descriptionRefs": []}]}
    r = app_with(ScriptLLM(ans)).post(URL, json={"fileUrl": SIGNED})
    assert r.json()["status"] == "SUCCEEDED" and r.json()["warnings"] == [{"code": ti.CHECKLIST_NO_BOX, "title": "개요"}]


# ---- 실패는 200 + FAILED + errorCode

@pytest.mark.parametrize("error, code", [(IngestError(URL_EXPIRED), URL_EXPIRED), (IngestError(DOWNLOAD_FAILED, "HTTP 404"), DOWNLOAD_FAILED),
                                         (IngestError(TOO_LARGE), TOO_LARGE)])
def test_파일을_못_받으면_FAILED와_그_코드(fetched, error, code):
    fetched["data"] = error
    llm = ScriptLLM()
    r = app_with(llm).post(URL, json={"requestId": "r-2", "fileUrl": SIGNED})
    assert r.status_code == 200 and r.json() == {"requestId": "r-2", "status": "FAILED", "errorCode": code}
    assert llm.calls == []                                     # 받지 못했으니 AI 는 부르지 않는다


def test_못_읽는_형식은_AI를_부르지_않고_FAILED(fetched):
    fetched["data"] = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    llm = ScriptLLM()
    r = app_with(llm).post(URL, json={"fileUrl": SIGNED})
    assert r.json() == {"status": "FAILED", "errorCode": UNSUPPORTED_FORMAT} and llm.calls == []


def test_AI가_실패하면_코드로_알린다(fetched):
    r = app_with(ScriptLLM(LLMError("x"), LLMError("x"))).post(URL, json={"fileUrl": SIGNED})
    assert r.json() == {"status": "FAILED", "errorCode": LLM_FAILED}
    bad = {"header": [], "footer": [], "fields": [{"ref": 9999, "title": "없는 줄", "type": "free", "columns": None, "descriptionRefs": []}]}
    r = app_with(ScriptLLM(bad, bad, bad)).post(URL, json={"fileUrl": SIGNED})
    assert r.json() == {"status": "FAILED", "errorCode": CHECK_FAILED}


def test_항목을_하나도_못_찾으면_NO_FIELDS(fetched):
    r = app_with(ScriptLLM({"header": [], "footer": [], "fields": []})).post(URL, json={"fileUrl": SIGNED})
    assert r.json() == {"status": "FAILED", "errorCode": ti.NO_FIELDS}


def test_예상_못_한_오류도_서버가_죽지_않고_internal_error(fetched, monkeypatch):
    def boom(*a, **k):
        raise KeyError("원문이 섞인 메시지")
    monkeypatch.setattr(api, "import_template", boom)
    r = app_with(ScriptLLM()).post(URL, json={"fileUrl": SIGNED})
    assert r.status_code == 200 and r.json() == {"status": "FAILED", "errorCode": INTERNAL_ERROR}


# ---- 요청 형식 오류 (400) 와 준비 안 됨 (503)

@pytest.mark.parametrize("body, word", [
    ({}, "fileUrl"),
    ({"fileUrl": ""}, "fileUrl"),
    ({"fileUrl": 3}, "fileUrl"),
    ({"requestId": 5, "fileUrl": SIGNED}, "requestId"),
    ({"requestId": "x" * 201, "fileUrl": SIGNED}, "requestId"),
    ([SIGNED], "JSON 객체"),
    ({"fileUrl": f"ftp://{HOST}/a.pdf"}, "http 또는 https"),
    ({"fileUrl": "https://evil.example/a.pdf?sig=SECRET-SIG"}, "허용한 저장소"),
    ({"fileUrl": f"https://user:pw@{HOST}/a.pdf"}, "계정 정보"),
])
def test_요청이_틀리면_400이고_주소를_글에_넣지_않는다(fetched, body, word):
    r = app_with(ScriptLLM()).post(URL, json=body)
    assert r.status_code == 400 and r.json()["error"] == "INVALID_INPUT" and word in r.json()["detail"]
    assert "SECRET-SIG" not in r.text and fetched["urls"] == []


def test_파일_받기가_꺼져_있으면_503_NOT_READY(fetched):
    off = UploadSettings(allowed_hosts=None, max_bytes=0, timeout_s=0)
    r = app_with(ScriptLLM(), upload=off).post(URL, json={"fileUrl": SIGNED})
    assert r.status_code == 503 and r.json()["error"] == "NOT_READY"


def test_AI_설정이_틀리면_503_NOT_READY(fetched):
    assert app_with(None).post(URL, json={"fileUrl": SIGNED}).status_code == 503
    r = app_with(ScriptLLM(), config_error="지시문 오류").post(URL, json={"fileUrl": SIGNED})
    assert r.status_code == 503 and r.json()["error"] == "NOT_READY" and "지시문" not in r.text


def test_동시에_처리하는_수가_가득_차면_503_BUSY(fetched):
    gate, started = threading.Event(), threading.Event()

    class SlowLLM(ScriptLLM):
        def generate(self, system, user, timeout_s, effort=None):
            started.set()
            gate.wait(5)
            return super().generate(system, user, timeout_s, effort)

    client = app_with(SlowLLM(report_answer()), max_concurrent=1)
    first = {}
    t = threading.Thread(target=lambda: first.update(r=client.post(URL, json={"fileUrl": SIGNED})))
    t.start()
    assert started.wait(5)
    busy = client.post(URL, json={"fileUrl": SIGNED})
    gate.set()
    t.join(5)
    assert busy.status_code == 503 and busy.json()["error"] == "BUSY"
    assert first["r"].json()["status"] == "SUCCEEDED"                # 먼저 들어온 것은 끝까지 처리
    assert client.post(URL, json={"fileUrl": SIGNED}).status_code in (200,)      # 자리가 비면 다시 받는다


# ---- 로그

def test_로그에_주소와_양식_글을_남기지_않는다(fetched, caplog):
    caplog.set_level(logging.INFO)
    app_with(ScriptLLM(report_answer())).post(URL, json={"requestId": "r-9", "fileUrl": SIGNED}, headers={"X-Request-Id": "t-9"})
    app_with(ScriptLLM()).post(URL, json={"fileUrl": "https://evil.example/a.pdf?sig=SECRET-SIG"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "r-9" in text and "t-9" in text and "status=SUCCEEDED" in text and "fields=1" in text
    for secret in ("SECRET-SIG", "X-Amz-Signature", "합성 자료", "개요"):
        assert secret not in text


# ---- 서버에 붙이는 곳 (setup)

def test_setup이_주소를_붙인다_설정이_틀려도_서버는_뜬다():
    pytest.importorskip("psycopg_pool")
    from minutes.runtime import setup
    env = {"MINUTES_DATABASE_URL": "postgresql://x:y@localhost:1/z", "MINUTES_LLM_PROVIDER": "fake",
           "RAG_UPLOAD_ALLOWED_HOSTS": HOST}
    app = FastAPI()
    setup(app, env)
    assert URL in app.openapi()["paths"]
    ok = TestClient(app).post(URL, json={"fileUrl": "https://evil.example/a.pdf"})        # 허용하지 않은 저장소 -> 400 (준비는 됨)
    assert ok.status_code == 400
    app2 = FastAPI()
    setup(app2, dict(env, MINUTES_IMPORT_PROMPT_VERSION="v999"))                           # 지시문 버전이 틀림 -> 이 주소만 503
    r = TestClient(app2).post(URL, json={"fileUrl": SIGNED})
    assert r.status_code == 503 and r.json()["error"] == "NOT_READY"
    assert "/internal/minutes/drafts" in app2.openapi()["paths"]                    # 초안 주소는 그대로
    app3 = FastAPI()
    setup(app3, dict(env, RAG_UPLOAD_ALLOWED_HOSTS=""))                                    # 저장소 설정 없음 -> 503
    assert TestClient(app3).post(URL, json={"fileUrl": SIGNED}).status_code == 503
