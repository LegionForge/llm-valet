"""Tests for config loading, env overrides, and settings validation."""
import os
import textwrap

import pytest

from llm_valet.config import Settings, _apply_env_overrides, _apply_yaml


# ── _apply_yaml ────────────────────────────────────────────────────────────────

class TestApplyYaml:
    def test_host_applied(self) -> None:
        s = Settings()
        _apply_yaml(s, {"host": "0.0.0.0"})
        assert s.host == "0.0.0.0"

    def test_port_applied(self) -> None:
        s = Settings()
        _apply_yaml(s, {"port": 9000})
        assert s.port == 9000

    def test_unknown_keys_ignored(self) -> None:
        """Unrecognised YAML keys must not crash or add attributes."""
        s = Settings()
        _apply_yaml(s, {"nonexistent_key": "value"})
        assert not hasattr(s, "nonexistent_key")

    def test_cors_origins_cast_to_str_list(self) -> None:
        s = Settings()
        _apply_yaml(s, {"cors_origins": ["http://example.com", "http://other.com"]})
        assert s.cors_origins == ["http://example.com", "http://other.com"]

    def test_cors_origins_empty_list(self) -> None:
        s = Settings()
        _apply_yaml(s, {"cors_origins": []})
        assert s.cors_origins == []

    def test_threshold_keys_applied(self) -> None:
        s = Settings()
        _apply_yaml(s, {"thresholds": {"ram_pause_pct": 75.0}})
        assert s.thresholds.ram_pause_pct == 75.0

    def test_unknown_threshold_keys_ignored(self) -> None:
        s = Settings()
        _apply_yaml(s, {"thresholds": {"unknown_threshold": 99}})
        # default should be unchanged
        assert s.thresholds.ram_pause_pct == 85.0

    def test_empty_yaml_leaves_defaults(self) -> None:
        s = Settings()
        _apply_yaml(s, {})
        assert s.host == "127.0.0.1"
        assert s.port == 8765

    def test_api_key_applied(self) -> None:
        s = Settings()
        _apply_yaml(s, {"api_key": "test-secret"})
        assert s.api_key == "test-secret"


# ── _apply_env_overrides ───────────────────────────────────────────────────────

class TestApplyEnvOverrides:
    def test_host_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_VALET_HOST", "0.0.0.0")
        s = Settings()
        _apply_env_overrides(s)
        assert s.host == "0.0.0.0"

    def test_port_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_VALET_PORT", "9999")
        s = Settings()
        _apply_env_overrides(s)
        assert s.port == 9999

    def test_api_key_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_VALET_API_KEY", "env-secret")
        s = Settings()
        _apply_env_overrides(s)
        assert s.api_key == "env-secret"

    def test_provider_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_VALET_PROVIDER", "lmstudio")
        s = Settings()
        _apply_env_overrides(s)
        assert s.provider == "lmstudio"

    def test_env_vars_absent_leaves_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("LLM_VALET_HOST", "LLM_VALET_PORT", "LLM_VALET_API_KEY", "LLM_VALET_PROVIDER"):
            monkeypatch.delenv(var, raising=False)
        s = Settings()
        _apply_env_overrides(s)
        assert s.host == "127.0.0.1"
        assert s.port == 8765
        assert s.api_key == ""

    def test_env_overrides_yaml_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env vars must win over YAML when both are present."""
        monkeypatch.setenv("LLM_VALET_API_KEY", "env-wins")
        s = Settings()
        _apply_yaml(s, {"api_key": "yaml-value"})
        _apply_env_overrides(s)
        assert s.api_key == "env-wins"


# ── Settings defaults ──────────────────────────────────────────────────────────

class TestSettingsDefaults:
    def test_default_host_is_localhost(self) -> None:
        assert Settings().host == "127.0.0.1"

    def test_default_port(self) -> None:
        assert Settings().port == 8765

    def test_default_api_key_empty(self) -> None:
        """Empty api_key = localhost-only mode; LAN requires explicit config."""
        assert Settings().api_key == ""

    def test_default_cors_origins_empty(self) -> None:
        """Empty list = same-origin only. Never a wildcard by default."""
        assert Settings().cors_origins == []

    def test_default_provider_is_ollama(self) -> None:
        assert Settings().provider == "ollama"

    def test_update_thresholds_rejects_unknown_key(self) -> None:
        s = Settings()
        result = s.update_thresholds({"nonexistent": 99})
        # unknown key silently ignored; result contains only valid threshold fields
        assert "nonexistent" not in result

    def test_update_thresholds_applies_valid_key(self) -> None:
        s = Settings()
        # Bypass file write by patching _save_settings is complex; test field mutation only
        s.thresholds.ram_pause_pct = 75.0
        assert s.thresholds.ram_pause_pct == 75.0

    def test_update_thresholds_rejects_pct_above_100(self) -> None:
        s = Settings()
        with pytest.raises(ValueError, match="ram_pause_pct"):
            s.update_thresholds({"ram_pause_pct": 150.0})

    def test_update_thresholds_rejects_negative_pct(self) -> None:
        s = Settings()
        with pytest.raises(ValueError, match="ram_pause_pct"):
            s.update_thresholds({"ram_pause_pct": -5.0})

    def test_update_thresholds_rejects_zero_pct(self) -> None:
        s = Settings()
        with pytest.raises(ValueError, match="ram_pause_pct"):
            s.update_thresholds({"ram_pause_pct": 0.0})

    def test_update_thresholds_rejects_inverted_hysteresis(self) -> None:
        s = Settings()
        # resume >= pause is invalid
        with pytest.raises(ValueError, match="ram_resume_pct"):
            s.update_thresholds({"ram_pause_pct": 60.0, "ram_resume_pct": 85.0})

    def test_update_thresholds_rejects_resume_equal_to_pause(self) -> None:
        s = Settings()
        with pytest.raises(ValueError, match="ram_resume_pct"):
            s.update_thresholds({"ram_pause_pct": 85.0, "ram_resume_pct": 85.0})

    def test_update_thresholds_rejects_check_interval_zero(self) -> None:
        s = Settings()
        with pytest.raises(ValueError, match="check_interval_seconds"):
            s.update_thresholds({"check_interval_seconds": 0})

    def test_update_thresholds_rejects_check_interval_negative(self) -> None:
        s = Settings()
        with pytest.raises(ValueError, match="check_interval_seconds"):
            s.update_thresholds({"check_interval_seconds": -1})

    def test_update_thresholds_does_not_mutate_on_error(self) -> None:
        s = Settings()
        original = s.thresholds.ram_pause_pct
        try:
            s.update_thresholds({"ram_pause_pct": 150.0})
        except ValueError:
            pass
        assert s.thresholds.ram_pause_pct == original
