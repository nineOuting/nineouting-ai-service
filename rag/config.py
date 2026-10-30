"""RAG 고정 설정.

모델 revision 과 체크섬은 2026-10-16 로컬 허깅페이스 캐시에서 잰 값이다.
"""

MODEL_NAME = "dragonkue/multilingual-e5-small-ko"
MODEL_REVISION = "bba4cb8e3299d445c04f62bf62e2b26461d03715"
MODEL_SHA256 = {
    "model.safetensors": "44f418d707a01d4fb443823f5df26757f905cedf450a4c9c48860ade42d0630b",
    "tokenizer.json": "ef04f2b385d1514f500e779207ace0f53e30895ce37563179e29f4022d28ca38",
}
# fetch_model 은 이 revision 의 파일을 전부 받는다 (2026-10-21 확인: 11개, 변환본 등 큰 파일 없음).
# 일부만 받으면 huggingface_hub 가 오프라인에서 "불완전한 캐시"로 보고 불러오기를 거부한다 (IncompleteSnapshotError)
EMBED_DIM = 384
MAX_TOKENS = 512          # 모델 입력 상한. 임베딩 직전에 넘지 않는지 검사한다 (접두어·특수 토큰 포함)
CHUNK_TOKENS = 256        # 조각 하나의 상한. 512 와 비교해 정함 (SCRUM-110: 업로드 문서 검색이 같거나 좋아지고
                          # 임베딩 시간이 줄어듦. 회의록 평가 질문은 결과가 같음)
MAX_DOC_TOKENS = 100_000  # 문서 하나의 조각 토큰 합계 상한. 넘으면 임베딩 전에 too_large (RAG_MAX_DOC_TOKENS 로 바꿈).
                          # 임베딩 시간은 토큰 수에 비례한다 (SCRUM-110: 개발 노트북 CPU 2개로 초당 약 6,700~9,300).
                          # 합성 문서로 PDF 약 145쪽, 엑셀 약 2,000행. 개발 노트북에서 약 15초, 배포 서버는 약 30초로
                          # 추정(노트북 절반 속도 가정). 서버 속도는 처리 완료 로그의 tokens·걸린시간으로 보고 조정
