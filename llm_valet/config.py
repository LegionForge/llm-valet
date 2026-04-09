import logging
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from llm_valet.resources.base import ResourceThresholds

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path("~/.llm-valet/config.yaml").expanduser()


@dataclass
class Settings:
    # Service
    host: str = "127.0.0.1"
    port: int = 8765
    # Provider
    provider: str = "ollama"
    ollama_url: str = "http://127.0.0.1:11434"
    model_name: str | None = None
    # Auth
    api_key: str = ""
    # CORS / trusted hosts
    cors_origins: list[str] = field(default_factory=list)
    extra_allowed_hosts: list[str] = field(default_factory=list)
    # Thresholds
    thresholds: ResourceThresholds = field(default_factory=ResourceThresholds)
    # Logging
    log_file: str = "~/.llm-valet/valet.log"

    def update_thresholds(self, data: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial threshold update and persist to disk."""
        allowed = {f.name for f in ResourceThresholds.__dataclass_fields__.values()}
        for key, value in data.items():
            if key in allowed:
                setattr(self.thresholds, key, value)
            else:
                safe_key = str(key).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
                logger.warning("unknown threshold key ignored", extra={"key": safe_key})
        _save_settings(self)
        return asdict(self.thresholds)


def load_settings() -> Settings:
    """Load settings from config.yaml, with env var overrides."""
    settings = Settings()

    if _CONFIG_PATH.is_file():
        _check_config_permissions(_CONFIG_PATH)
        try:
            with _CONFIG_PATH.open("r", encoding="utf-8") as f:
                raw: dict[str, Any] = yaml.safe_load(f) or {}
            _apply_yaml(settings, raw)
        except yaml.YAMLError as exc:
            logger.error("failed to parse config.yaml", extra={"error": str(exc)})

    _apply_env_overrides(settings)
    return settings


def _apply_yaml(settings: Settings, raw: dict[str, Any]) -> None:
    for key in ("host", "port", "provider", "ollama_url", "model_name", "api_key", "log_file"):
        if key in raw:
            setattr(settings, key, raw[key])

    if "cors_origins" in raw:
        settings.cors_origins = [str(x) for x in raw["cors_origins"]]
    if "extra_allowed_hosts" in raw:
        settings.extra_allowed_hosts = [str(x) for x in raw["extra_allowed_hosts"]]

    if "thresholds" in raw and isinstance(raw["thresholds"], dict):
        allowed = set(ResourceThresholds.__dataclass_fields__)
        for key, value in raw["thresholds"].items():
            if key in allowed:
                setattr(settings.thresholds, key, value)


def _apply_env_overrides(settings: Settings) -> None:
    if val := os.environ.get("LLM_VALET_HOST"):
        settings.host = val
    if val := os.environ.get("LLM_VALET_PORT"):
        settings.port = int(val)
    if val := os.environ.get("LLM_VALET_API_KEY"):
        settings.api_key = val
    if val := os.environ.get("LLM_VALET_PROVIDER"):
        settings.provider = val


def _save_settings(settings: Settings) -> None:
    """Persist settings to config.yaml with 0600 permissions."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        "host": settings.host,
        "port": settings.port,
        "provider": settings.provider,
        "ollama_url": settings.ollama_url,
        "model_name": settings.model_name,
        "api_key": settings.api_key,
        "cors_origins": settings.cors_origins,
        "extra_allowed_hosts": settings.extra_allowed_hosts,
        "log_file": settings.log_file,
        "thresholds": asdict(settings.thresholds),
    }
    with _CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, default_flow_style=False)

    # Enforce 0600 — api_key must not be world-readable (T8)
    try:
        _CONFIG_PATH.chmod(0o600)
    except OSError:
        logger.warning("could not set config.yaml to 0600 — check permissions")


def _check_config_permissions(path: Path) -> None:
    """Warn on startup if config.yaml is world-readable."""
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            logger.warning(
                "config.yaml is readable by group/other — consider chmod 0600",
                extra={"path": str(path)},
            )
    except OSError:
        pass
