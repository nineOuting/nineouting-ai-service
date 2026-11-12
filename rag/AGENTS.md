# rag/AGENTS.md

`rag/` 폴더(RAG: 문서 넣기·검색·답변) 작업 규칙이다. 저장소 공통 규칙(브랜치, 커밋 메시지, 비밀키)은 최상단 README.md를 따른다.

## 범위

- RAG 코드, 시험, 마이그레이션, 의존성은 모두 `rag/` 안에 둔다.
- `rag/` 밖의 파일은 수정하지 않는다. 공용 파일(`Dockerfile`, `requirements.txt` 등)을 바꿔야 하면 담당자와 먼저 합의한다.
- 예외: `shared/`는 RAG와 회의록 초안(`minutes/`)이 같이 쓰는 코드(AI 연결, 완료 콜백)다. 고치면 두 기능의 시험을 모두 돌린다. 요약 기능(`summary/`, `server/`)은 대상이 아니다.
- 브랜치는 `develop`에서 `feature/rag-*`로 만든다.

## 데이터와 로그

- 원본 파일, 사용자 정보, 질문과 답변은 저장하지 않는다.
- 로그에 질문, 문서 글, 회의록 내용을 남기지 않는다. ID, 개수, 상태, 오류 코드, 예외 이름만 남긴다.
- 실제 회의 데이터는 올리지 않는다. 시험 샘플은 합성 데이터로 `rag/tests/fixtures/`에 둔다.

## API 규칙

- 필드는 camelCase, 시간은 밀리초, ID는 정수로 쓴다.
- 문서는 `sourceType`(회의록 `minutes` / 첨부 자료 `upload`)과 `docId`의 짝으로 구분한다. 회의록 200번과 첨부 자료 200번은 다른 문서다.
- 검색은 항상 `workspaceId`와 문서 짝 목록(`docs`)으로 거른다. `docs`가 비면 결과 없음으로 답하고, 필터를 건너뛰지 않는다.
- 같은 문서(`sourceType`+`docId`)의 같은 `docVersion`이 다시 와도 결과는 하나다. 버전 교체(옛 조각 삭제와 새 조각 삽입)는 한 트랜잭션에서 하므로, 교체가 끝나기 전까지 옛 버전이 검색된다.
- 답변 API는 근거 조각이 없으면 LLM을 부르지 않고 `no_result`로 답한다. 답 속 `[n]`이 넘긴 조각을 가리키는지 코드로 검사한다.
- 회의 중 챗봇은 근거가 없어도 LLM을 부른다. 일반적인 개념·용어·방법과 날짜는 일반 지식으로 답하고 `[일반]`을 붙인다. 회의·회사 사실(일정, 담당자, 금액, 결정 등)은 근거 없이 답하지 않는다. `[n]` 검사는 같다.

## 실행 환경

- Python은 저장소의 `.venv`를 쓰고, 버전은 `Dockerfile`과 같은 3.12에 맞춘다.
- 의존성은 `rag/requirements.txt`에 둔다. 공용 코드(`shared/`)가 쓰는 것은 `shared/requirements.txt`에 둔다. 공용 `requirements.txt`와 합치는 방식은 따로 정한다.
- 시험: `.venv/bin/python -m pytest rag/tests shared/tests`
