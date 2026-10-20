-- RAG 표 3개. 명세(CN팀에 넘긴 것) 기준이며, 명세에 없던 부분은 ★ 로 표시한다.
-- vector 확장은 관리자가 미리 만든다 (로컬: db/init/01_roles.sql).

CREATE TABLE rag_documents (
  doc_id       bigint      PRIMARY KEY,
  doc_version  text        NOT NULL,                       -- 마지막으로 접수한 버전
  workspace_id bigint      NOT NULL,
  source_type  text        NOT NULL CHECK (source_type IN ('upload', 'minutes')),            -- ★ CHECK
  title        text        NOT NULL,
  status       text        NOT NULL CHECK (status IN ('queued', 'processing', 'ready', 'failed')),  -- ★ 값 목록
  error_code   text,
  chunk_count  int         NOT NULL DEFAULT 0,
  embed_model  text,                                       -- ready 인 조각을 만든 모델 (모델이름@revision)
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rag_documents_workspace_idx ON rag_documents (workspace_id);   -- ★ 워크스페이스 지우기용

CREATE TABLE rag_chunks (
  id           bigserial   PRIMARY KEY,
  doc_id       bigint      NOT NULL REFERENCES rag_documents (doc_id) ON DELETE CASCADE,  -- ★ 외래키
  doc_version  text        NOT NULL,
  workspace_id bigint      NOT NULL,
  chunk_index  int         NOT NULL,
  content      text        NOT NULL,
  location     jsonb       NOT NULL,
  token_count  int         NOT NULL,
  embedding    vector(384) NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (doc_id, doc_version, chunk_index)
);
CREATE INDEX rag_chunks_workspace_doc_idx ON rag_chunks (workspace_id, doc_id);
-- HNSW(코사인) 인덱스는 데이터가 쌓이면 추가한다. 검색은 workspace_id·doc_id 로 먼저 좁히므로 처음에는 전부 비교한다.

-- 대기열. 쓰는 코드는 대기열·작업자 단계에서 만든다.
CREATE TABLE rag_jobs (
  id           bigserial   PRIMARY KEY,
  doc_id       bigint      NOT NULL,
  doc_version  text        NOT NULL,
  payload      jsonb       NOT NULL,
  status       text        NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
  attempts     int         NOT NULL DEFAULT 0,
  request_id   text,
  last_error   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rag_jobs_queued_idx ON rag_jobs (id) WHERE status = 'queued';   -- ★ FOR UPDATE SKIP LOCKED 로 꺼낼 때
