"""
API integration tests — create_app() wired to a real OllamaProvider, no HTTP mocks.

Uses TestClient (sync) with a module-scoped app so the watchdog starts once per
module. check_interval=300 prevents the watchdog from auto-ticking during tests.

Run: pytest -m integration tests/integration/test_api_live.py
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from llm_valet.api import create_app
from llm_valet.config import Settings
from llm_valet.resources.base import ResourceThresholds

# Ollama's /api/ps lags ~1 s after keep_alive=0 eviction — set expires_at to
# near-future rather than clearing immediately.  Sync tests must sleep before
# asserting model_loaded==False.
_EVICTION_SETTLE_S = 1.5

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("ollama_url")]

_TEST_API_KEY = "integration-test-key"


def _make_settings(model_name: str) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8765,
        # "testserver" is the Host header Starlette's TestClient sends by default;
        # TrustedHostMiddleware rejects any other value unless it is in this list.
        extra_allowed_hosts=["testserver"],
        # TestClient reports client.host as "testclient" (not 127.0.0.1), so the
        # localhost auth bypass never fires — inject the key on every request instead.
        api_key=_TEST_API_KEY,
        model_name=model_name,
        thresholds=ResourceThresholds(check_interval_seconds=300),
    )


class _AuthClient:
    """Wraps TestClient and injects X-API-Key on every request."""

    def __init__(self, tc: TestClient) -> None:
        self._tc = tc

    def _h(self, kw: dict) -> dict:
        h = dict(kw.pop("headers", {}) or {})
        h["X-API-Key"] = _TEST_API_KEY
        return h

    def get(self, url: str, **kw):  # type: ignore[no-untyped-def]
        return self._tc.get(url, headers=self._h(kw), **kw)

    def post(self, url: str, **kw):  # type: ignore[no-untyped-def]
        return self._tc.post(url, headers=self._h(kw), **kw)

    def put(self, url: str, **kw):  # type: ignore[no-untyped-def]
        return self._tc.put(url, headers=self._h(kw), **kw)

    def delete(self, url: str, **kw):  # type: ignore[no-untyped-def]
        return self._tc.delete(url, headers=self._h(kw), **kw)


@pytest.fixture(scope="module")
def client(test_model: str) -> _AuthClient:  # type: ignore[misc]
    """
    Module-scoped app client backed by a real OllamaProvider.
    Force-pauses on setup so every test module starts with no model loaded.
    """
    settings = _make_settings(test_model)
    with patch("llm_valet.api._configure_logging"):
        app = create_app(settings)
    with TestClient(app, raise_server_exceptions=True) as tc:
        wrapped = _AuthClient(tc)
        wrapped.post("/pause/force")  # clean starting state
        yield wrapped


# ── Read-only endpoints ───────────────────────────────────────────────────────


class TestReadOnlyEndpoints:
    def test_status_top_level_schema(self, client: _AuthClient) -> None:
        r = client.get("/status")
        assert r.status_code == 200
        body = r.json()
        for key in ("provider", "metrics", "watchdog", "version", "security"):
            assert key in body, f"missing key: {key}"

    def test_status_provider_running(self, client: _AuthClient) -> None:
        body = client.get("/status").json()
        assert body["provider"]["running"] is True

    def test_metrics_top_level_schema(self, client: _AuthClient) -> None:
        r = client.get("/metrics")
        assert r.status_code == 200
        body = r.json()
        for key in ("memory", "cpu", "gpu", "disk"):
            assert key in body, f"missing key: {key}"

    def test_metrics_memory_values_in_range(self, client: _AuthClient) -> None:
        mem = client.get("/metrics").json()["memory"]
        assert mem["total_mb"] > 0
        assert 0.0 <= mem["used_pct"] <= 100.0
        assert mem["pressure"] in ("normal", "warn", "critical")

    def test_watchdog_schema(self, client: _AuthClient) -> None:
        r = client.get("/watchdog")
        assert r.status_code == 200
        body = r.json()
        assert "state" in body and "last_reason" in body
        assert body["state"] in ("running", "pausing", "paused", "resuming", "provider_down")

    def test_models_includes_test_model(self, client: _AuthClient, test_model: str) -> None:
        r = client.get("/models")
        assert r.status_code == 200
        names = [m["name"] for m in r.json()["models"]]
        assert any(test_model in name for name in names)

    def test_config_get_schema(self, client: _AuthClient) -> None:
        r = client.get("/config")
        assert r.status_code == 200
        body = r.json()
        for key in ("ram_pause_pct", "ram_resume_pct", "cpu_pause_pct", "check_interval_seconds"):
            assert key in body, f"missing key: {key}"

    def test_wrong_api_key_returns_401(self, client: _AuthClient) -> None:
        r = client._tc.get("/status", headers={"X-API-Key": "wrong-key"})
        assert r.status_code == 401


# ── State-changing: pause / resume cycle ──────────────────────────────────────


class TestPauseResumeCycle:
    def test_load_model(self, client: _AuthClient, test_model: str) -> None:
        r = client.post("/load", json={"model": test_model})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_pause_unloads_model(self, client: _AuthClient) -> None:
        r = client.post("/pause")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        time.sleep(_EVICTION_SETTLE_S)
        status = client.get("/status").json()
        assert status["provider"]["model_loaded"] is False
        assert status["watchdog"]["state"] == "paused"

    def test_resume_reloads_model(self, client: _AuthClient) -> None:
        r = client.post("/resume")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        status = client.get("/status").json()
        assert status["provider"]["model_loaded"] is True
        assert status["watchdog"]["state"] == "running"

    def test_force_pause(self, client: _AuthClient) -> None:
        # Rate limiter on /resume is 2s; sleep to ensure the cooldown from
        # test_resume_reloads_model has cleared before calling /resume again.
        time.sleep(2.1)
        # Resume first so there is something to force-pause
        client.post("/resume")
        # Poll until /api/ps confirms the model is loaded before force-pausing
        for _ in range(20):
            if client.get("/status").json()["provider"]["model_loaded"]:
                break
            time.sleep(0.25)
        r = client.post("/pause/force")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        time.sleep(_EVICTION_SETTLE_S)
        status = client.get("/status").json()
        assert status["provider"]["model_loaded"] is False


# ── Config round-trip ─────────────────────────────────────────────────────────


class TestConfig:
    def test_config_round_trip(self, client: _AuthClient) -> None:
        original = client.get("/config").json()
        new_val = round(original["ram_pause_pct"] - 5.0, 1)
        assert client.put("/config", json={"ram_pause_pct": new_val}).status_code == 200
        assert client.get("/config").json()["ram_pause_pct"] == new_val
        client.put("/config", json={"ram_pause_pct": original["ram_pause_pct"]})

    def test_inverted_thresholds_rejected(self, client: _AuthClient) -> None:
        r = client.put("/config", json={"ram_pause_pct": 60.0, "ram_resume_pct": 85.0})
        assert r.status_code in (400, 422)

    def test_out_of_range_threshold_rejected(self, client: _AuthClient) -> None:
        r = client.put("/config", json={"ram_pause_pct": 150.0})
        assert r.status_code in (400, 422)
