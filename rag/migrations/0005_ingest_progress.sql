-- 문서 넣기 진행률 (SCRUM-339). 상태 조회 응답의 progress(0~100)를 이 두 값으로 계산한다.
--   embed_done   임베딩이 끝난 조각 수. 작업자가 묶음마다 고친다
--   embed_total  지금 버전의 전체 조각 수. 나누기 전에는 0
-- 넣기를 접수하면(새 문서·새 버전) 둘 다 0으로 돌린다. ready 는 이 값과 관계없이 100 이라 들어 있는 문서는 0 으로 둔다.

ALTER TABLE rag_documents
  ADD COLUMN embed_done  int NOT NULL DEFAULT 0,
  ADD COLUMN embed_total int NOT NULL DEFAULT 0;
