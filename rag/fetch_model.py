"""임베딩 모델을 revision 고정으로 받아 허깅페이스 캐시에 넣고 sha256 을 확인한다.

실행: .venv/bin/python -m rag.fetch_model
이미 있고 체크섬이 맞으면 접속하지 않고 끝낸다.
받는 위치는 huggingface_hub 규칙을 따른다 (HF_HUB_CACHE > HF_HOME/hub > ~/.cache/huggingface/hub).
"""
from .config import MODEL_NAME, MODEL_REVISION
from .embedding import verify_model_files


def main():
    try:
        verify_model_files()
        print(f"모델이 이미 있음: {MODEL_NAME}@{MODEL_REVISION[:7]}")
        return
    except (OSError, RuntimeError):
        pass

    from huggingface_hub import snapshot_download

    snapshot_download(MODEL_NAME, revision=MODEL_REVISION)   # 일부만 받지 않는다 (config.py 주석)
    verify_model_files()                       # 체크섬이 다르면 RuntimeError
    print(f"모델 받기 완료: {MODEL_NAME}@{MODEL_REVISION[:7]}")


if __name__ == "__main__":
    main()
