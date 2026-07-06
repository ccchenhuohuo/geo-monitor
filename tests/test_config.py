from pathlib import Path

from geo_monitor.config import Settings, get_settings, redact_secret, validate_live_settings


def test_default_settings_do_not_load_cwd_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEO_MONITOR_ENV_FILE", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    (tmp_path / ".env").write_text("LLM_BASE_URL=https://evil.example/v1\nLLM_API_KEY=secret\n", encoding="utf-8")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.llm_base_url == "https://api.example.com/v1"
    assert settings.api_key_status == "missing"


def test_explicit_geo_monitor_env_file_is_loaded(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_BASE_URL=https://provider.example/v1/\nLLM_API_KEY=secret\n", encoding="utf-8")
    monkeypatch.setenv("GEO_MONITOR_ENV_FILE", str(env_file))
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.llm_base_url == "https://provider.example/v1"
    assert settings.has_api_key is True
    assert settings.redacted()["CONFIG_ENV_FILE"] == str(env_file.resolve())
    assert settings.redacted()["LLM_BASE_URL_SOURCE"] == "GEO_MONITOR_ENV_FILE"


def test_live_endpoint_status_and_validation():
    assert Settings(llm_api_key="secret").llm_base_url_status == "placeholder"
    assert Settings(llm_api_key="secret", llm_base_url="not-a-url").llm_base_url_status == "invalid"
    assert Settings(llm_api_key="secret", llm_base_url="https://provider.example/v1").llm_base_url_status == "configured"

    for settings in [
        Settings(llm_api_key=None, llm_base_url="https://provider.example/v1"),
        Settings(llm_api_key="sk-xxxx", llm_base_url="https://provider.example/v1"),
        Settings(llm_api_key="secret"),
        Settings(llm_api_key="secret", llm_base_url="not-a-url"),
    ]:
        try:
            validate_live_settings(settings)
        except ValueError:
            pass
        else:
            raise AssertionError("expected live settings validation error")

    validate_live_settings(Settings(llm_api_key="secret", llm_base_url="https://provider.example/v1"))


def test_redact_secret_keeps_key_out_of_errors():
    settings = Settings(llm_api_key="super-secret", llm_base_url="https://provider.example/v1")

    assert redact_secret("token=super-secret", settings) == "token=***"
