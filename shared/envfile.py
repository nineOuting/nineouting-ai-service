"""로컬 설정 파일(.env) 읽기. 손 시험·평가 도구만 쓴다. 서버는 파일을 읽지 않고 환경변수만 본다.

값을 고르는 순서 (앞이 이긴다):
  1. 환경변수
  2. 저장소 루트 .env
  3. rag/.env  옛 위치. 옮기는 동안만 읽고, 여기서 값을 가져오면 경고한다

값은 출력하지 않는다 (경고에는 이름만).
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT_ENV = REPO / ".env"
OLD_ENV = REPO / "rag" / ".env"


def load_env_file(path) -> dict:
    """`이름=값` 줄만 읽는다. 주석·빈 줄·값이 빈 줄은 건너뛴다 (예시 파일을 복사한 빈 칸이 다른 값을 가리지 않게)."""
    env = {}
    path = Path(path)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v:
                    env[k] = v
    return env


def local_env(environ=None, root=None, old=None) -> dict:
    """환경변수 > 루트 .env > rag/.env 를 합친 dict. 기본 경로는 부를 때 읽는다 (시험에서 바꿔 끼울 수 있게)."""
    environ = os.environ if environ is None else environ
    root_values = load_env_file(ROOT_ENV if root is None else root)
    old_values = load_env_file(OLD_ENV if old is None else old)
    used_old = sorted(k for k in old_values if k not in root_values and k not in environ)
    if used_old:
        print(f"경고: rag/.env 는 옛 위치다. 그 줄들을 저장소 루트 .env 로 옮긴다 "
              f"(루트 .env 가 아직 없을 때만 cp rag/.env .env). rag/.env 에서 읽은 이름: {', '.join(used_old)}",
              file=sys.stderr)
    return {**old_values, **root_values, **environ}
