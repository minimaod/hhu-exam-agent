"""DeepSeek 本地配置的读取顺序、错误映射与启动快照。"""

from pathlib import Path

import pytest

import app.config as query_config
from app.config import (
    QueryConfigError,
    load_config_state,
    load_query_settings,
)


def write_env(tmp_path: Path, content: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    return path


def test_missing_key_raises() -> None:
    with pytest.raises(QueryConfigError) as excinfo:
        load_query_settings()

    assert "DEEPSEEK_API_KEY" in str(excinfo.value)


def test_env_file_is_loaded_with_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_env(
        tmp_path,
        "DEEPSEEK_API_KEY=file-key\nDEEPSEEK_MODEL=custom-model\n",
    )
    monkeypatch.setattr(query_config, "ENV_FILE", path)

    settings = load_query_settings()

    assert settings.api_key == "file-key"
    assert settings.model == "custom-model"
    assert settings.base_url == "https://api.deepseek.com"


def test_existing_environment_wins_over_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_env(tmp_path, "DEEPSEEK_API_KEY=file-key\n")
    monkeypatch.setattr(query_config, "ENV_FILE", path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")

    assert load_query_settings().api_key == "env-key"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://[broken",
        "http://example.test:bad",
        "ftp://example.test",
        "not-a-url",
        "http://",
        "http://exa mple.com",
    ],
)
def test_malformed_base_url_raises(
    base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", base_url)

    with pytest.raises(QueryConfigError) as excinfo:
        load_query_settings()

    assert "DEEPSEEK_BASE_URL" in str(excinfo.value)


def test_ipv6_base_url_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://[::1]:8000/")

    assert load_query_settings().base_url == "http://[::1]:8000"


def test_invalid_utf8_env_file_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"DEEPSEEK_API_KEY=\xff\xfe\n")
    monkeypatch.setattr(query_config, "ENV_FILE", path)

    with pytest.raises(QueryConfigError) as excinfo:
        load_query_settings()

    assert "配置" in str(excinfo.value)


def test_environment_can_cover_broken_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"DEEPSEEK_API_KEY=\xff\xfe\n")
    monkeypatch.setattr(query_config, "ENV_FILE", path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")

    assert load_query_settings().api_key == "env-key"


def test_config_state_wraps_error_without_raising() -> None:
    state = load_config_state()

    assert state.settings is None
    assert state.error is not None
    assert "DEEPSEEK_API_KEY" in state.error
