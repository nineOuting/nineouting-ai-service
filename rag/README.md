# rag

RAG 파이프라인 (문서 넣기·검색·답변). 작업 규칙은 `AGENTS.md`.

## 현재 상태

| 단계 | 상태 |
|---|---|
| 회의록 로더, 512토큰 나누기, 임베딩 | 구현 |
| 저장(pgvector), 검색, 대기열, API, PDF·워드·엑셀, 답변 | 예정 |

## 준비 (저장소 루트에서)

Python 3.12 (`Dockerfile`과 같음)

```bash
uv venv --python 3.12 --seed .venv        # uv 가 없으면: python3.12 -m venv .venv
.venv/bin/pip install -r rag/requirements-dev.txt
```

임베딩 모델 (dragonkue/multilingual-e5-small-ko, 약 465MB, revision 고정)

```bash
.venv/bin/python -m rag.fetch_model
```

- 이미 있으면 접속하지 않는다. 받은 뒤 sha256 을 확인한다
- 서버와 시험은 모델을 받지 않는다. 캐시에 없으면 오류(시험은 건너뜀)

## 시험

```bash
.venv/bin/python -m pytest rag/tests
```

| 환경변수 | 설명 |
|---|---|
| `RAG_REQUIRE_MODEL=1` | 모델이 없으면 임베딩 시험을 건너뛰지 않고 실패시킨다 (CI 용) |
| `HF_HUB_CACHE` | 모델 캐시 위치. 없으면 `~/.cache/huggingface/hub`. 이미지에서는 `/models` 로 고정할 예정 (실행 사용자가 바뀌어도 같은 곳을 찾도록) |
