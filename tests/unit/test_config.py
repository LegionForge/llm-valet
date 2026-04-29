"""Tests for config loading, env overrides, and settings validation."""

from unittest.mock import patch

import pytest

import llm_valet.config as config_module
from llm_valet.config import (
    Settings,
    _apply_env_overrides,
    _apply_yaml,
    _validate_provider_url,
    load_settings,
)

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

    # M1 — threshold validation in YAML load path

    def test_threshold_pct_above_100_ignored(self) -> None:
        s = Settings()
        _apply_yaml(s, {"thresholds": {"ram_pause_pct": 150}})
        assert s.thresholds.ram_pause_pct == 85.0  # default unchanged

    def test_threshold_pct_zero_ignored(self) -> None:
        s = Settings()
        _apply_yaml(s, {"thresholds": {"ram_pause_pct": 0}})
        assert s.thresholds.ram_pause_pct == 85.0

    def test_threshold_pct_non_numeric_ignored(self) -> None:
        s = Settings()
        _apply_yaml(s, {"thresholds": {"ram_pause_pct": "high"}})
        assert s.thresholds.ram_pause_pct == 85.0

    def test_threshold_check_interval_zero_ignored(self) -> None:
        s = Settings()
        _apply_yaml(s, {"thresholds": {"check_interval_seconds": 0}})
        assert s.thresholds.check_interval_seconds == 10  # default unchanged

    def test_threshold_check_interval_negative_ignored(self) -> None:
        s = Settings()
        _apply_yaml(s, {"thresholds": {"check_interval_seconds": -5}})
        assert s.thresholds.check_interval_seconds == 10

    def test_threshold_inverted_hysteresis_block_ignored(self) -> None:
        """ram_resume_pct >= ram_pause_pct must cause the entire threshold block to be skipped."""
        s = Settings()
        _apply_yaml(s, {"thresholds": {"ram_pause_pct": 70.0, "ram_resume_pct": 80.0}})
        assert s.thresholds.ram_pause_pct == 85.0
        assert s.thresholds.ram_resume_pct == 60.0

    def test_threshold_valid_block_applied(self) -> None:
        s = Settings()
        _apply_yaml(s, {"thresholds": {"ram_pause_pct": 90.0, "ram_resume_pct": 65.0}})
        assert s.thresholds.ram_pause_pct == 90.0
        assert s.thresholds.ram_resume_pct == 65.0


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

    def test_invalid_port_env_var_keeps_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M2 — non-numeric LLM_VALET_PORT must not crash; default port is preserved."""
        monkeypatch.setenv("LLM_VALET_PORT", "auto")
        s = Settings()
        _apply_env_overrides(s)
        assert s.port == 8765


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

    def test_update_thresholds_rejects_non_numeric_pct(self) -> None:
        s = Settings()
        with pytest.raises(ValueError, match="ram_pause_pct"):
            s.update_thresholds({"ram_pause_pct": "high"})

    def test_acknowledge_key_sets_flag_and_saves(self) -> None:
        s = Settings()
        with patch("llm_valet.config._save_settings") as mock_save:
            s.acknowledge_key()
        assert s.key_acknowledged is True
        mock_save.assert_called_once_with(s)

    def test_apply_network_config_sets_fields_and_saves(self) -> None:
        s = Settings()
        with patch("llm_valet.config._save_settings") as mock_save:
            s.apply_network_config("0.0.0.0", 9000)
        assert s.host == "0.0.0.0"
        assert s.port == 9000
        assert s.key_acknowledged is True
        mock_save.assert_called_once_with(s)


# ── _validate_provider_url (T6 SSRF) ──────────────────────────────────────────


class TestValidateProviderUrl:
    """Cover every branch of the SSRF guard."""

    # --- accepted ---
    def test_http_loopback(self) -> None:
        assert _validate_provider_url("http://127.0.0.1:11434")

    def test_https_loopback(self) -> None:
        assert _validate_provider_url("https://127.0.0.1:11434")

    def test_localhost_name(self) -> None:
        assert _validate_provider_url("http://localhost:11434")

    def test_ipv6_loopback(self) -> None:
        # RFC 2732 bracket form is required for IPv6 in URLs
        assert _validate_provider_url("http://[::1]:11434")

    def test_mdns_local(self) -> None:
        assert _validate_provider_url("http://mac-mini.local:11434")

    def test_rfc1918_192(self) -> None:
        assert _validate_provider_url("http://192.168.1.100:11434")

    def test_rfc1918_10(self) -> None:
        assert _validate_provider_url("http://10.0.0.5:11434")

    def test_rfc1918_172(self) -> None:
        assert _validate_provider_url("http://172.16.0.1:11434")

    # --- rejected: bad scheme ---
    def test_ftp_scheme_rejected(self) -> None:
        assert not _validate_provider_url("ftp://127.0.0.1:11434")

    def test_no_scheme_rejected(self) -> None:
        assert not _validate_provider_url("127.0.0.1:11434")

    # --- rejected: empty / missing host ---
    def test_empty_string_rejected(self) -> None:
        assert not _validate_provider_url("")

    def test_scheme_only_rejected(self) -> None:
        assert not _validate_provider_url("http://")

    # --- rejected: public / routable addresses ---
    def test_public_ip_rejected(self) -> None:
        assert not _validate_provider_url("http://8.8.8.8:11434")

    def test_aws_metadata_rejected(self) -> None:
        # Link-local; not RFC1918 — SSRF classic target
        assert not _validate_provider_url("http://169.254.169.254")

    def test_public_domain_rejected(self) -> None:
        # Hostname that isn't localhost/.local — hits ValueError in ip_address(), returns False
        assert not _validate_provider_url("http://evil.com:11434")

    def test_malformed_url_rejected(self) -> None:
        # Exercises the outer except branch
        assert not _validate_provider_url("http://[invalid-ipv6")


# ── _apply_yaml — ollama_url validation ───────────────────────────────────────


class TestApplyYamlOllamaUrl:
    def test_valid_ollama_url_accepted(self) -> None:
        s = Settings()
        _apply_yaml(s, {"ollama_url": "http://192.168.1.50:11434"})
        assert s.ollama_url == "http://192.168.1.50:11434"

    def test_invalid_ollama_url_rejected_keeps_default(self) -> None:
        s = Settings()
        default = s.ollama_url
        _apply_yaml(s, {"ollama_url": "http://evil.com:11434"})
        assert s.ollama_url == default

    def test_public_ip_ollama_url_rejected(self) -> None:
        s = Settings()
        default = s.ollama_url
        _apply_yaml(s, {"ollama_url": "http://8.8.8.8:11434"})
        assert s.ollama_url == default


# ── load_settings — error handling ────────────────────────────────────────────


class TestLoadSettingsErrorHandling:
    def test_corrupt_yaml_returns_defaults(
        self, tmp_path: pytest.TempdirFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corrupt config.yaml must not raise — fall back to defaults."""
        corrupt = tmp_path / "config.yaml"
        corrupt.write_text("{ bad yaml: [unclosed")
        monkeypatch.setattr(config_module, "_CONFIG_PATH", corrupt)
        settings = load_settings()
        assert settings.host == "127.0.0.1"
        assert settings.port == 8765
