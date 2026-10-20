"""조각·질문 임베딩.

모델: dragonkue/multilingual-e5-small-ko (revision 은 config.py 에 고정)
e5 계열은 질문 앞에 "query: ", 조각 앞에 "passage: " 를 붙인다 (모델 카드 사용법).
벡터는 길이 1로 정규화한다. 그래서 코사인 거리와 내적 순위가 같다.
"""
import hashlib
from pathlib import Path
from typing import Protocol, Sequence

from .config import EMBED_DIM, MAX_TOKENS, MODEL_NAME, MODEL_REVISION, MODEL_SHA256


class Embedder(Protocol):
    model_id: str                # "모델이름@revision". 조각마다 저장한다
    dim: int
    max_tokens: int

    def passage_tokens(self, text: str) -> int: ...
    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class E5Embedder:
    PASSAGE = "passage: "
    QUERY = "query: "

    def __init__(self, name=MODEL_NAME, revision=MODEL_REVISION, device="cpu", threads=None,
                 batch_size=16, local_files_only=True):
        # torch 는 무거워서 임베더를 만들 때만 불러온다 (로더·나누기 시험은 torch 없이 돈다)
        import torch
        from sentence_transformers import SentenceTransformer

        if threads:
            torch.set_num_threads(threads)
        try:
            self._model = SentenceTransformer(name, revision=revision, device=device,
                                              local_files_only=local_files_only)
        except OSError as e:
            raise OSError(f"캐시에 모델 없음 ({name}@{revision[:7]}). "
                          "`.venv/bin/python -m rag.fetch_model` 로 받는다") from e
        self._tokenizer = self._model.tokenizer
        if self._model.max_seq_length < MAX_TOKENS:
            raise RuntimeError(f"모델 입력 상한 {self._model.max_seq_length} 이 {MAX_TOKENS} 보다 작음")
        self.model_id = f"{name}@{revision}"
        self.dim = EMBED_DIM
        self.max_tokens = MAX_TOKENS
        self.batch_size = batch_size

    def passage_tokens(self, text):
        return self._count(self.PASSAGE + text)

    def embed_passages(self, texts):
        inputs = [self.PASSAGE + t for t in texts]
        for i, t in enumerate(inputs):
            if self._count(t) > self.max_tokens:
                # 모델은 넘치는 뒤쪽을 말없이 자른다. 나누기 단계가 막았어야 하므로 오류로 본다
                raise ValueError(f"{i}번째 조각이 {self.max_tokens}토큰을 넘음")
        return self._encode(inputs)

    def embed_query(self, text):
        # 질문이 상한을 넘으면 모델이 뒤를 자른다. 질문은 짧다고 본다
        return self._encode([self.QUERY + text])[0]

    def _count(self, text):
        return len(self._tokenizer(text, add_special_tokens=True, truncation=False)["input_ids"])

    def _encode(self, inputs):
        vecs = self._model.encode(list(inputs), batch_size=self.batch_size, normalize_embeddings=True,
                                  convert_to_numpy=True, show_progress_bar=False)
        if vecs.shape[1] != self.dim:
            raise RuntimeError(f"벡터 차원 {vecs.shape[1]} 이 {self.dim} 이 아님")
        return vecs.tolist()


def verify_model_files(name=MODEL_NAME, revision=MODEL_REVISION):
    """캐시(또는 이미지)에 있는 모델 파일의 sha256 이 config 값과 같은지 확인한다. 내려받지 않는다."""
    from huggingface_hub import snapshot_download

    root = Path(snapshot_download(name, revision=revision, local_files_only=True))
    for fname, expected in MODEL_SHA256.items():
        h = hashlib.sha256()
        with open(root / fname, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != expected:
            raise RuntimeError(f"{fname} 체크섬이 다름")
