-- 회의록 초안 작업 대기열. 명세 2.3 (docs/api/minutes-draft-contract.md)
--   요청을 받으면 queued 로 넣고, 작업자가 하나씩 running 으로 꺼내 초안을 만든 뒤 결과를 콜백으로 보낸다.
--   status       queued(대기) / running(처리 중) / done(결과 콜백을 보냄) / failed(콜백을 끝내 못 보냄, 또는 시도 횟수 초과)
--   result_status 콜백으로 보낸 결과 SUCCEEDED / FAILED. 같은 request_id 가 다시 오면 SUCCEEDED 이거나
--                 처리 중일 때만 DUPLICATE 이고, 실패로 끝난 요청은 새로 받는다 (jobs.submit)
--   payload      요청 본문. 끝나면 비운다 (회의 내용을 남기지 않게)
--   trace_id     X-Request-Id. 로그와 콜백에 그대로 붙인다

CREATE TABLE minutes_jobs (
  id            bigserial   PRIMARY KEY,
  request_id    text        NOT NULL UNIQUE,
  minutes_id    bigint      NOT NULL,
  payload       jsonb       NOT NULL,
  callback_url  text        NOT NULL,
  trace_id      text,
  status        text        NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
  result_status text        CHECK (result_status IN ('SUCCEEDED', 'FAILED')),
  error_code    text,
  attempts      int         NOT NULL DEFAULT 0,
  last_error    text,
  run_after     timestamptz NOT NULL DEFAULT now(),
  started_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX minutes_jobs_ready_idx    ON minutes_jobs (run_after, id) WHERE status = 'queued';
CREATE INDEX minutes_jobs_running_idx  ON minutes_jobs (started_at)    WHERE status = 'running';
CREATE INDEX minutes_jobs_finished_idx ON minutes_jobs (updated_at)    WHERE status IN ('done', 'failed');
