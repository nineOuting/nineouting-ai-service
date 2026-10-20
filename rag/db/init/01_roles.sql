-- 컨테이너를 처음 띄울 때 관리자(postgres)로 한 번 실행된다.
-- 운영의 "RAG 전용 DB + 전용 계정"과 같은 조건: 앱 계정은 관리자 권한이 없고, 확장은 관리자가 만든다.
-- 비밀번호는 컨테이너 환경변수에서 읽는다 (파일·명령줄에 값을 남기지 않음).

\getenv rag_app_password RAG_APP_PASSWORD
CREATE ROLE rag_app LOGIN PASSWORD :'rag_app_password';

CREATE DATABASE rag OWNER rag_app;
CREATE DATABASE rag_test OWNER rag_app;

\connect rag
CREATE EXTENSION IF NOT EXISTS vector;

\connect rag_test
CREATE EXTENSION IF NOT EXISTS vector;
