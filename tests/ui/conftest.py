"""
Shared fixtures for WebUI (Playwright) tests.

Serves llm_valet/static/ via a simple HTTP server so index.html loads from
a real HTTP origin (not file://) — this ensures relative fetch() calls resolve
correctly and page.route() interception works reliably.

Requires:
    pip install playwright pytest-playwright
    playwright install chromium
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path
from typing import Any

import pytest

# ── Static file server ────────────────────────────────────────────────────────


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that serves from the static dir and suppresses logs."""

    _static_dir: str = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=self._static_dir, **kwargs)

    def log_message(self, *args: Any) -> None:  # suppress access logs in test output
        pass


@pytest.fixture(scope="session")
def static_server() -> str:  # type: ignore[return]
    """
    Spin up a one-shot HTTP server that serves llm_valet/static/ on a random
    localhost port.  Returns the base URL (e.g. 'http://127.0.0.1:54321').
    Session-scoped — one server per test run, shared across all UI tests.
    """
    static_dir = Path(__file__).parent.parent.parent / "llm_valet" / "static"

    # Bind a subclass so we can inject the directory without a lambda
    handler_cls = type(
        "_Handler",
        (_QuietHandler,),
        {"_static_dir": str(static_dir)},
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


# ── Mock API response builders ────────────────────────────────────────────────


def _status_response(
    model_loaded: bool = False,
    model_name: str | None = None,
    memory_used_mb: int | None = None,
    loaded_context_length: int | None = None,
) -> dict[str, Any]:
    return {
        "provider": {
            "running": True,
            "model_loaded": model_loaded,
            "model_name": model_name,
            "memory_used_mb": memory_used_mb,
            "size_vram_mb": None,
            "loaded_context_length": loaded_context_length,
            "endpoint": "http://127.0.0.1:11434",
            "overcommit": False,
        },
        "metrics": {
            "memory": {"total_mb": 16384, "used_mb": 8192, "used_pct": 50.0, "pressure": "normal"},
            "cpu": {"used_pct": 5.0, "core_count": 10},
            "gpu": {
                "available": False,
                "vram_total_mb": None,
                "vram_used_mb": None,
                "vram_used_pct": None,
                "compute_pct": None,
            },
            "disk": {
                "path": "/",
                "total_mb": 200000,
                "used_mb": 50000,
                "free_mb": 150000,
                "used_pct": 25.0,
            },
            "timestamp": 1776350000.0,
        },
        "watchdog": {"state": "running", "last_reason": ""},
        "version": "0.5.3",
        "security": {"lan_exposed": False, "auth_enabled": False},
    }


def _config_response() -> dict[str, Any]:
    return {
        "ram_pause_pct": 85,
        "ram_resume_pct": 60,
        "cpu_pause_pct": 90,
        "gpu_vram_pause_pct": 85,
        "auto_resume_on_ram_pressure": True,
        "check_interval_seconds": 10,
    }


def _models_response(models: list[dict[str, Any]]) -> dict[str, Any]:
    return {"models": models}


# ── Page fixture with API mocked ──────────────────────────────────────────────


def make_api_mock(page: Any, models: list[dict[str, Any]]) -> None:
    """
    Install page.route() handlers that intercept all API calls from the WebUI.
    Must be called before page.goto().
    """
    page.route(
        "**/status",
        lambda route: route.fulfill(
            content_type="application/json",
            body=json.dumps(_status_response()),
        ),
    )
    page.route(
        "**/config",
        lambda route: route.fulfill(
            content_type="application/json",
            body=json.dumps(_config_response()),
        ),
    )
    page.route(
        "**/models",
        lambda route: route.fulfill(
            content_type="application/json",
            body=json.dumps(_models_response(models)),
        ),
    )
