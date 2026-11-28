"""새 모델이 temperature를 받지 않아도 LLM 호출이 실패하지 않는다."""

import pytest

from app.mapping.llm import create_with_fallback


class _Rejected(Exception):
    status_code = 400

    def __str__(self):
        return "Unsupported value: 'temperature' does not support 0 with this model."


def test_retries_without_temperature_when_model_rejects_it():
    calls = []

    def create(**kw):
        calls.append(dict(kw))
        if "temperature" in kw:
            raise _Rejected()
        return "ok"

    assert create_with_fallback(create, model="gpt-6.2-sol", temperature=0, messages=[]) == "ok"
    assert [("temperature" in c) for c in calls] == [True, False]


def test_other_errors_are_not_swallowed():
    class Other(Exception):
        status_code = 401

    def create(**kw):
        raise Other("bad key")

    with pytest.raises(Other):
        create_with_fallback(create, model="m", temperature=0, messages=[])


def test_default_model_is_gpt_6_2_sol(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setattr("app.config.load_env_file", lambda *a, **k: None)
    assert Settings.from_env().openai_model == "gpt-6.2-sol"
