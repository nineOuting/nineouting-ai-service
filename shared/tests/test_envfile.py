"""로컬 설정 파일 읽기 시험. 실제 .env 는 읽지 않는다 (임시 폴더의 파일을 넘긴다)."""
from shared.envfile import load_env_file, local_env


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_이름_값_줄만_읽고_주석_빈_줄_빈_값은_건너뜀(tmp_path):
    f = write(tmp_path / ".env", '# 주석\n\nA=1\n B = "두 칸" \nC=\'셋\'\nD=\n# E=5\n이상한 줄\n')
    assert load_env_file(f) == {"A": "1", "B": "두 칸", "C": "셋"}


def test_파일이_없으면_빈_dict(tmp_path):
    assert load_env_file(tmp_path / "없음") == {}


def test_환경변수가_루트보다_루트가_옛_위치보다_우선(tmp_path, capsys):
    root = write(tmp_path / "root.env", "A=루트\nB=루트\n")
    old = write(tmp_path / "old.env", "A=옛\nB=옛\nC=옛\n")
    env = local_env({"A": "환경"}, root=root, old=old)
    assert (env["A"], env["B"], env["C"]) == ("환경", "루트", "옛")


def test_옛_위치에서_값을_가져오면_이름만_경고(tmp_path, capsys):
    root = write(tmp_path / "root.env", "A=루트\n")
    old = write(tmp_path / "old.env", "A=옛\nRAG_LLM_MODEL=비밀아님\nOPENAI_API_KEY=sk-값\n")
    local_env({}, root=root, old=old)
    err = capsys.readouterr().err
    assert "rag/.env 는 옛 위치다" in err
    assert "OPENAI_API_KEY, RAG_LLM_MODEL" in err                 # 루트가 가린 A 는 없음
    assert "sk-값" not in err and "비밀아님" not in err             # 값은 출력하지 않음


def test_옛_위치의_값이_모두_가려지면_경고하지_않음(tmp_path, capsys):
    root = write(tmp_path / "root.env", "A=루트\n")
    old = write(tmp_path / "old.env", "A=옛\nB=옛\n")
    local_env({"B": "환경"}, root=root, old=old)
    assert capsys.readouterr().err == ""


def test_루트_빈_칸은_옛_위치_값을_가리지_않음(tmp_path, capsys):
    """루트 .env.example 을 복사만 하고 채우지 않은 칸(A=)이 rag/.env 의 값을 지우지 않게."""
    root = write(tmp_path / "root.env", "A=\n")
    old = write(tmp_path / "old.env", "A=옛\n")
    assert local_env({}, root=root, old=old)["A"] == "옛"


def test_둘_다_없으면_환경변수만(tmp_path, capsys):
    assert local_env({"A": "1"}, root=tmp_path / "x", old=tmp_path / "y") == {"A": "1"}
    assert capsys.readouterr().err == ""
