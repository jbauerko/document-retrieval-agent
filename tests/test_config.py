import pytest

from document_retrieval_agent.config import ModelConfig
from document_retrieval_agent.model import DEFAULT_API_BASE, DEFAULT_MODEL


@pytest.fixture(autouse=True)
def clean_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in [
        "DOCUMENT_RETRIEVAL_AGENT_MODEL",
        "DOCUMENT_RETRIEVAL_AGENT_API_BASE",
        "DOCUMENT_RETRIEVAL_AGENT_API_KEY",
        "GEMINI_API_KEY",
    ]:
        monkeypatch.delenv(name, raising=False)


def test_default_configuration_loads_dotenv_without_exposing_key(tmp_path):
    (tmp_path / ".env").write_text("GEMINI_API_KEY=test-secret\n")
    config = ModelConfig.from_environment()
    assert config.model == DEFAULT_MODEL
    assert config.api_base == DEFAULT_API_BASE
    assert config.api_key == "test-secret"
    assert "test-secret" not in repr(config)


def test_explicit_settings_override_environment(monkeypatch):
    monkeypatch.setenv("DOCUMENT_RETRIEVAL_AGENT_MODEL", "environment-model")
    monkeypatch.setenv("DOCUMENT_RETRIEVAL_AGENT_API_BASE", "https://environment.example/v1")
    monkeypatch.setenv("DOCUMENT_RETRIEVAL_AGENT_API_KEY", "test-provider-key")
    config = ModelConfig.from_environment("argument-model", "https://argument.example/v1")
    assert config.model == "argument-model"
    assert config.api_base == "https://argument.example/v1"
    assert config.api_key == "test-provider-key"


def test_gemini_key_is_not_forwarded_to_a_custom_endpoint(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    with pytest.raises(ValueError, match="DOCUMENT_RETRIEVAL_AGENT_API_KEY"):
        ModelConfig.from_environment(api_base="https://other-provider.example/v1")
