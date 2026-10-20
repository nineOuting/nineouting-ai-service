-- 작업자(SCRUM-69)용 rag_jobs 칸 추가. 명세에 없던 칸이다.
--   run_after    재시도할 때 이 시각 이후에만 꺼낸다 (뒤로 미루기)
--   started_at   running 으로 잡힌 시각. 작업자가 죽어 멈춘 작업을 다시 잡을 때 쓴다
--   workspace_id 워크스페이스를 지울 때 대기 중인 작업도 함께 지우려고

ALTER TABLE rag_jobs
  ADD COLUMN workspace_id bigint,
  ADD COLUMN run_after    timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN started_at   timestamptz;

DROP INDEX rag_jobs_queued_idx;
CREATE INDEX rag_jobs_ready_idx     ON rag_jobs (run_after, id) WHERE status = 'queued';
CREATE INDEX rag_jobs_running_idx   ON rag_jobs (started_at)    WHERE status = 'running';
CREATE INDEX rag_jobs_finished_idx  ON rag_jobs (updated_at)    WHERE status IN ('done', 'failed');
CREATE INDEX rag_jobs_doc_idx       ON rag_jobs (doc_id);
CREATE INDEX rag_jobs_workspace_idx ON rag_jobs (workspace_id);
