-- 문서 날짜 (SCRUM-234). AI 에 근거를 넘길 때 어느 쪽이 최근인지 알 수 있게 한다.
--   meeting_started_at  회의록의 meetingStartAt. 받은 시각 그대로 (시간대가 붙어 와도 벽시계 시각만 둔다). 첨부 자료는 NULL
--   received_at         지금 버전을 받은 시각. 첨부 자료는 이것을 "올린 날"로 쓴다
-- 들어 있는 문서: received_at 은 마지막으로 바뀐 시각(updated_at)으로 채운다 (근사값).
-- meeting_started_at 은 다음에 다시 넣을 때 채워진다.

ALTER TABLE rag_documents
  ADD COLUMN meeting_started_at timestamp,
  ADD COLUMN received_at        timestamptz;
UPDATE rag_documents SET received_at = updated_at;
ALTER TABLE rag_documents
  ALTER COLUMN received_at SET NOT NULL,
  ALTER COLUMN received_at SET DEFAULT now();
