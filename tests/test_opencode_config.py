"""OpenCode isolation: app-owned HOME, no user profile, no cloud providers."""
from __future__ import annotations

from local_coder.engines.opencode_config import (
    CLOUD_ENV_KEYS,
    DISABLED_PROVIDERS,
    isolated_env,
    opencode_config,
    overlay_content,
    write_engine_home,
)
from local_coder.engines.opencode_pin import PINNED_SHA256, PINNED_VERSION, pin_record


def test_pin_identity_is_recorded():
    record = pin_record()
    assert record["version"] == PINNED_VERSION
    assert record["sha256"] == PINNED_SHA256
    assert record["license"] == "MIT"


def test_config_is_local_only():
    cfg = opencode_config(llama_base_url="http://127.0.0.1:8080")
    assert cfg["enabled_providers"] == ["llama.cpp"]
    assert cfg["model"].startswith("llama.cpp/")
    assert cfg["small_model"] == cfg["model"]
    assert cfg["autoupdate"] is False
    assert cfg["share"] == "disabled"
    assert cfg["permission"]["webfetch"] == "deny"
    assert cfg["permission"]["websearch"] == "deny"
    assert cfg["permission"]["external_directory"] == "deny"
    assert "openai" in DISABLED_PROVIDERS
    assert "anthropic" in DISABLED_PROVIDERS


def test_isolated_env_does_not_inherit_cloud_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-user")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-home" / ".config"))
    env = isolated_env()
    assert env["HOME"].endswith("local-coder/engine-home") or "engine-home" in env["HOME"]
    assert env["XDG_CONFIG_HOME"].endswith(".config")
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    for key in CLOUD_ENV_KEYS:
        assert key not in env
    user_config = tmp_path / "user-home" / ".config" / "opencode"
    assert not str(env["XDG_CONFIG_HOME"]).startswith(str(user_config))


def test_overlay_wins_on_cloud_keys():
    cfg = opencode_config(llama_base_url="http://127.0.0.1:8080")
    overlay = overlay_content(cfg)
    assert '"enabled_providers": ["llama.cpp"]' in overlay.replace(" ", "") or "llama.cpp" in overlay
    assert "webfetch" in overlay


def test_write_engine_home_does_not_touch_user_opencode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    cfg = opencode_config(llama_base_url="http://127.0.0.1:8080")
    path = write_engine_home(cfg)
    assert "engine-home" in str(path)
    assert not (tmp_path / "real-home" / ".config" / "opencode" / "opencode.json").exists()
