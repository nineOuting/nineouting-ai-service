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
MAX_TOKENS = 512          # 조각 하나의 임베딩 입력 상한 (접두어·특수 토큰 포함)
