from app.config import load_env_file


def test_env_file_loaded_without_overriding(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# c\nOPENAI_MODEL=from-file\nMAPPER=rules\nEMPTY=\nexport SPRING_URL='http://x'\n")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("SPRING_URL", raising=False)
    monkeypatch.setenv("MAPPER", "openai")
    monkeypatch.delenv("EMPTY", raising=False)
    load_env_file(f)
    import os
    assert os.environ["OPENAI_MODEL"] == "from-file"
    assert os.environ["MAPPER"] == "openai"          # 이미 있는 값은 유지
    assert os.environ["SPRING_URL"] == "http://x"
    assert "EMPTY" not in os.environ
