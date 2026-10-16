"""RAG 고정 설정.

모델 revision 과 체크섬은 2026-10-16 로컬 허깅페이스 캐시에서 잰 값이다.
"""

MODEL_NAME = "dragonkue/multilingual-e5-small-ko"
MODEL_REVISION = "bba4cb8e3299d445c04f62bf62e2b26461d03715"
MODEL_SHA256 = {
    "model.safetensors": "44f418d707a01d4fb443823f5df26757f905cedf450a4c9c48860ade42d0630b",
    "tokenizer.json": "ef04f2b385d1514f500e779207ace0f53e30895ce37563179e29f4022d28ca38",
}
EMBED_DIM = 384
MAX_TOKENS = 512          # 조각 하나의 임베딩 입력 상한 (접두어·특수 토큰 포함)
