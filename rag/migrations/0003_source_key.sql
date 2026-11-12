-- 문서를 (source_type, doc_id) 짝으로 구분한다 (SCRUM-227, 회의 중 챗봇 명세 v0.2 9장).
-- 회의록 번호와 첨부 자료 번호는 백엔드에서 따로 매겨 겹칠 수 있다. 지금은 doc_id 하나가 기본 키라,
-- 같은 번호의 회의록과 첨부 자료가 들어오면 나중 것이 앞의 것을 덮어쓴다.
-- 들어 있는 데이터는 그대로 옮긴다: 조각·작업에 문서의 source_type 을 채운다.

-- 조각
ALTER TABLE rag_chunks ADD COLUMN source_type text;
UPDATE rag_chunks c SET source_type = d.source_type FROM rag_documents d WHERE d.doc_id = c.doc_id;
ALTER TABLE rag_chunks ALTER COLUMN source_type SET NOT NULL;
ALTER TABLE rag_chunks DROP CONSTRAINT rag_chunks_doc_id_fkey;
ALTER TABLE rag_chunks DROP CONSTRAINT rag_chunks_doc_id_doc_version_chunk_index_key;

-- 문서: 기본 키를 짝으로
ALTER TABLE rag_documents DROP CONSTRAINT rag_documents_pkey;
ALTER TABLE rag_documents ADD CONSTRAINT rag_documents_pkey PRIMARY KEY (source_type, doc_id);

ALTER TABLE rag_chunks ADD CONSTRAINT rag_chunks_doc_fkey
  FOREIGN KEY (source_type, doc_id) REFERENCES rag_documents (source_type, doc_id) ON DELETE CASCADE;
ALTER TABLE rag_chunks ADD CONSTRAINT rag_chunks_doc_version_chunk_key
  UNIQUE (source_type, doc_id, doc_version, chunk_index);
DROP INDEX rag_chunks_workspace_doc_idx;
CREATE INDEX rag_chunks_workspace_doc_idx ON rag_chunks (workspace_id, source_type, doc_id);

-- 작업: 대기 중인 작업은 payload 에 sourceType 이 있다. 끝난 작업은 payload 를 비웠으므로 문서에서 찾고,
-- 문서도 지워졌으면 minutes 로 둔다 (끝난 작업 줄은 로그 확인용이라 처리에 영향이 없다)
ALTER TABLE rag_jobs ADD COLUMN source_type text;
UPDATE rag_jobs j SET source_type = COALESCE(
  j.payload ->> 'sourceType',
  (SELECT d.source_type FROM rag_documents d WHERE d.doc_id = j.doc_id),
  'minutes');
ALTER TABLE rag_jobs ALTER COLUMN source_type SET NOT NULL;
ALTER TABLE rag_jobs ADD CONSTRAINT rag_jobs_source_type_check CHECK (source_type IN ('upload', 'minutes'));
DROP INDEX rag_jobs_doc_idx;
CREATE INDEX rag_jobs_doc_idx ON rag_jobs (source_type, doc_id);
