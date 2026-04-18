"""
API endpoint integration tests — mock provider + collector, no real HTTP to Ollama.

Strategy: patch _build_provider, _build_collector, _check_not_root, and
_configure_logging so create_app() runs without any real services. The
TestClient handles the FastAPI lifespan (starts/stops the watchdog task).
A 300-second check_interval keeps the watchdog from ticking during tests.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from llm_valet.api import _VERSION, create_app
from llm_valet.config import Settings
from llm_valet.providers.base import ModelInfo, ProviderStatus
from llm_valet.resources.base import (
    CPUMetrics,
    DiskMetrics,
    GPUMetrics,
    MemoryMetrics,
    PressureLevel,
    ResourceThresholds,
    SystemMetrics,
)
from llm_valet.watchdog import WatchdogState


# ── Factories ─────────────────────────────────────────────────────────────────

def _make_metrics(ram_pct: float = 50.0) -> SystemMetrics:
    return SystemMetrics(
        memory=MemoryMetrics(
            total_mb=16384,
            used_mb=int(16384 * ram_pct / 100),
            used_pct=ram_pct,
            pressure=PressureLevel.NORMAL,
        ),
        cpu=CPUMetrics(used_pct=5.0, core_count=8),
        gpu=GPUMetrics(
            available=False,
            vram_total_mb=None,
            vram_used_mb=None,
            vram_used_pct=None,
            compute_pct=None,
        ),
        disk=DiskMetrics(path="/", total_mb=512000, used_mb=128000, free_mb=384000, used_pct=25.0),
    )


def _make_provider_status(
    running: bool = True,
    model_loaded: bool = True,
    model_name: str | None = "test:model",
    memory_used_mb: int | None = 2000,
    loaded_context_length: int | None = 4096,
) -> ProviderStatus:
    return ProviderStatus(
        running=running,
        model_loaded=model_loaded,
        model_name=model_name,
        memory_used_mb=memory_used_mb,
        loaded_context_length=loaded_context_length,
    )


def _make_mock_provider(status: ProviderStatus | None = None) -> MagicMock:
    p = MagicMock()
    p.status   = AsyncMock(return_value=status or _make_provider_status())
    p.pause    = AsyncMock(return_value=True)
    p.resume   = AsyncMock(return_value=True)
    p.stop     = AsyncMock(return_value=True)
    p.start    = AsyncMock(return_value=True)
    p.health_check  = AsyncMock(return_value=True)
    p.list_models   = AsyncMock(return_value=[
        ModelInfo(name="test:model", size_mb=2000, context_length=32768),
    ])
    p.load_model    = AsyncMock(return_value=True)
    p.delete_model  = AsyncMock(return_value=True)
    p.pull_model    = AsyncMock(return_value=True)
    p.force_pause   = AsyncMock(return_value=True)
    return p


def _make_mock_collector() -> MagicMock:
    c = MagicMock()
    c.collect          = MagicMock(return_value=_make_metrics())
    c.supported_metrics = MagicMock(return_value={"memory", "cpu", "gpu", "disk"})
    return c


_TEST_API_KEY = "test-api-key"


def _make_test_settings(**overrides: object) -> Settings:
    """Settings that point to localhost with a very long watchdog interval."""
    s = Settings(
        host="127.0.0.1",
        port=8765,
        # "testserver" is the Host header Starlette TestClient sends by default.
        # Without it TrustedHostMiddleware returns 400 on every request.
        extra_allowed_hosts=["testserver"],
        # TestClient always reports client host as "testclient" (not 127.0.0.1),
        # so set an api_key and inject it via _AuthClient on every request.
        api_key=_TEST_API_KEY,
        thresholds=ResourceThresholds(check_interval_seconds=300),
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class _AuthClient:
    """
    Thin wrapper around TestClient that injects X-API-Key on every request.

    Starlette's TestClient always reports request.client.host as "testclient",
    not "127.0.0.1", so the localhost auth bypass never fires. Setting an
    api_key in test settings + injecting it here is the clean alternative
    to patching internals or modifying production auth code.
    """

    def __init__(self, tc: TestClient) -> None:
        self._tc = tc

    def _headers(self, kw: dict) -> dict:
        h = dict(kw.pop("headers", {}) or {})
        h["X-API-Key"] = _TEST_API_KEY
        return h

    def get(self, url: str, **kw: object):
        return self._tc.get(url, headers=self._headers(kw), **kw)

    def post(self, url: str, **kw: object):
        return self._tc.post(url, headers=self._headers(kw), **kw)

    def put(self, url: str, **kw: object):
        return self._tc.put(url, headers=self._headers(kw), **kw)

    def delete(self, url: str, **kw: object):
        return self._tc.delete(url, headers=self._headers(kw), **kw)


# ── Fixture ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def api(request: pytest.FixtureRequest):
    """
    Yields (_AuthClient, mock_provider, mock_collector).

    _AuthClient wraps TestClient and injects X-API-Key on every request so
    the auth middleware passes even though TestClient reports client host as
    "testclient" rather than "127.0.0.1".
    """
    mock_provider  = _make_mock_provider()
    mock_collector = _make_mock_collector()
    settings = _make_test_settings()

    with (
        patch("llm_valet.api._build_provider",  return_value=mock_provider),
        patch("llm_valet.api._build_collector", return_value=mock_collector),
        patch("llm_valet.api._check_not_root"),
        patch("llm_valet.api._configure_logging"),
        # Keep process_iter mocked for the entire TestClient lifetime, not just
        # create_app(). The watchdog background task calls _detect_game() on its
        # first tick (before the first asyncio.sleep). Without this patch the real
        # psutil runs and any steamapps/common process on the host machine causes
        # an unexpected auto-pause, breaking state assertions in several tests.
        patch("llm_valet.watchdog.psutil.process_iter", return_value=[]),
    ):
        app = create_app(settings)
        with TestClient(app, raise_server_exceptions=True) as tc:
            yield _AuthClient(tc), mock_provider, mock_collector


# ── GET /status ───────────────────────────────────────────────────────────────

class TestGetStatus:
    def test_returns_200(self, api: tuple) -> None:
        client, _, _ = api
        r = client.get("/status")
        assert r.status_code == 200

    def test_response_has_provider_key(self, api: tuple) -> None:
        client, _, _ = api
        data = client.get("/status").json()
        assert "provider" in data

    def test_response_has_metrics_key(self, api: tuple) -> None:
        client, _, _ = api
        data = client.get("/status").json()
        assert "metrics" in data

    def test_response_has_watchdog_key(self, api: tuple) -> None:
        client, _, _ = api
        data = client.get("/status").json()
        assert "watchdog" in data

    def test_response_has_version_field(self, api: tuple) -> None:
        client, _, _ = api
        data = client.get("/status").json()
        assert "version" in data
        assert data["version"] == _VERSION

    def test_provider_running_and_loaded(self, api: tuple) -> None:
        client, mock_provider, _ = api
        mock_provider.status.return_value = _make_provider_status(
            running=True, model_loaded=True, model_name="llama3:latest"
        )
        data = client.get("/status").json()
        assert data["provider"]["running"] is True
        assert data["provider"]["model_loaded"] is True
        assert data["provider"]["model_name"] == "llama3:latest"

    def test_provider_stopped(self, api: tuple) -> None:
        client, mock_provider, _ = api
        mock_provider.status.return_value = _make_provider_status(
            running=False, model_loaded=False, model_name=None, memory_used_mb=None
        )
        data = client.get("/status").json()
        assert data["provider"]["running"] is False

    def test_provider_idle_running_no_model(self, api: tuple) -> None:
        """Service up + no model → watchdog state determines IDLE vs PAUSED in UI."""
        client, mock_provider, _ = api
        mock_provider.status.return_value = _make_provider_status(
            running=True, model_loaded=False, model_name=None, memory_used_mb=None
        )
        data = client.get("/status").json()
        assert data["provider"]["running"] is True
        assert data["provider"]["model_loaded"] is False

    def test_watchdog_state_running_on_start(self, api: tuple) -> None:
        client, _, _ = api
        data = client.get("/status").json()
        assert data["watchdog"]["state"] == WatchdogState.RUNNING.value

    def test_loaded_context_length_returned(self, api: tuple) -> None:
        client, mock_provider, _ = api
        mock_provider.status.return_value = _make_provider_status(
            loaded_context_length=4096
        )
        data = client.get("/status").json()
        assert data["provider"]["loaded_context_length"] == 4096

    def test_metrics_memory_fields(self, api: tuple) -> None:
        client, _, _ = api
        data = client.get("/status").json()
        mem = data["metrics"]["memory"]
        assert "used_pct" in mem
        assert "total_mb" in mem
        assert "pressure" in mem

    def test_overcommit_false_when_model_fits_in_threshold(self, api: tuple) -> None:
        """Default fixture: 2000 MB model on 16384 MB machine, ram_pause_pct=85 → no overcommit."""
        client, _, _ = api
        data = client.get("/status").json()
        # 16384 * 0.85 = 13926 MB threshold; 2000 MB model is well within it
        assert data["provider"]["overcommit"] is False

    def test_overcommit_true_when_model_exceeds_threshold(self, api: tuple) -> None:
        """Model larger than ram_pause_pct of total RAM → overcommit flagged."""
        client, mock_provider, _ = api
        # 16384 MB total, ram_pause_pct=85 → threshold = 13926 MB
        # Simulate a 26 GB model (like mistral-nemo:12b at 128K ctx on Mac Mini)
        mock_provider.status.return_value = _make_provider_status(memory_used_mb=26624)
        data = client.get("/status").json()
        assert data["provider"]["overcommit"] is True

    def test_overcommit_false_when_no_model_loaded(self, api: tuple) -> None:
        client, mock_provider, _ = api
        mock_provider.status.return_value = _make_provider_status(
            model_loaded=False, model_name=None, memory_used_mb=None
        )
        data = client.get("/status").json()
        assert data["provider"]["overcommit"] is False

    def test_overcommit_false_when_memory_used_mb_is_none(self, api: tuple) -> None:
        """Provider loaded but memory_used_mb not yet reported → no false positive."""
        client, mock_provider, _ = api
        mock_provider.status.return_value = _make_provider_status(memory_used_mb=None)
        data = client.get("/status").json()
        assert data["provider"]["overcommit"] is False


# ── POST /pause ───────────────────────────────────────────────────────────────

class TestPostPause:
    def test_returns_200(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/pause")
        assert r.status_code == 200

    def test_calls_provider_pause(self, api: tuple) -> None:
        client, mock_provider, _ = api
        client.post("/pause")
        mock_provider.pause.assert_called_once()

    def test_response_ok_true_on_success(self, api: tuple) -> None:
        client, mock_provider, _ = api
        mock_provider.pause.return_value = True
        data = client.post("/pause").json()
        assert data["ok"] is True

    def test_response_ok_false_on_failure(self, api: tuple) -> None:
        client, mock_provider, _ = api
        mock_provider.pause.return_value = False
        data = client.post("/pause").json()
        assert data["ok"] is False

    def test_watchdog_synced_after_pause(self, api: tuple) -> None:
        """After successful pause, watchdog state must be PAUSED."""
        client, mock_provider, _ = api
        mock_provider.pause.return_value = True
        client.post("/pause")
        data = client.get("/status").json()
        assert data["watchdog"]["state"] == WatchdogState.PAUSED.value


# ── POST /resume ──────────────────────────────────────────────────────────────

class TestPostResume:
    def test_returns_200(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/resume")
        assert r.status_code == 200

    def test_calls_provider_resume(self, api: tuple) -> None:
        client, mock_provider, _ = api
        client.post("/resume")
        mock_provider.resume.assert_called_once()

    def test_watchdog_running_after_successful_resume(self, api: tuple) -> None:
        client, mock_provider, _ = api
        mock_provider.pause.return_value  = True
        mock_provider.resume.return_value = True
        client.post("/pause")
        client.post("/resume")
        data = client.get("/status").json()
        assert data["watchdog"]["state"] == WatchdogState.RUNNING.value


# ── POST /stop — non-blocking (BackgroundTasks) ───────────────────────────────

class TestPostStop:
    def test_returns_200_immediately(self, api: tuple) -> None:
        """stop is non-blocking — must return before provider.stop() completes."""
        client, _, _ = api
        r = client.post("/stop")
        assert r.status_code == 200

    def test_response_has_ok_and_action(self, api: tuple) -> None:
        client, _, _ = api
        data = client.post("/stop").json()
        assert "ok" in data
        assert data["action"] == "stop"


# ── POST /start — non-blocking ────────────────────────────────────────────────

class TestPostStart:
    def test_returns_200_immediately(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/start")
        assert r.status_code == 200

    def test_response_action_is_start(self, api: tuple) -> None:
        client, _, _ = api
        data = client.post("/start").json()
        assert data["action"] == "start"


# ── POST /restart — non-blocking ──────────────────────────────────────────────

class TestPostRestart:
    def test_returns_200_immediately(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/restart")
        assert r.status_code == 200

    def test_response_action_is_restart(self, api: tuple) -> None:
        client, _, _ = api
        data = client.post("/restart").json()
        assert data["action"] == "restart"


# ── GET /models ───────────────────────────────────────────────────────────────

class TestGetModels:
    def test_returns_200(self, api: tuple) -> None:
        client, _, _ = api
        assert client.get("/models").status_code == 200

    def test_returns_models_list(self, api: tuple) -> None:
        client, _, _ = api
        data = client.get("/models").json()
        assert "models" in data
        assert isinstance(data["models"], list)

    def test_model_has_name_and_size(self, api: tuple) -> None:
        client, _, _ = api
        models = client.get("/models").json()["models"]
        assert len(models) == 1
        assert models[0]["name"] == "test:model"
        assert models[0]["size_mb"] == 2000


# ── POST /load ────────────────────────────────────────────────────────────────

class TestPostLoad:
    def test_returns_200(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/load", json={"model": "test:model"})
        assert r.status_code == 200

    def test_calls_provider_load_model(self, api: tuple) -> None:
        client, mock_provider, _ = api
        client.post("/load", json={"model": "test:model"})
        mock_provider.load_model.assert_called_once_with("test:model", num_ctx=None)

    def test_calls_provider_load_model_with_num_ctx(self, api: tuple) -> None:
        client, mock_provider, _ = api
        client.post("/load", json={"model": "test:model", "num_ctx": 4096})
        mock_provider.load_model.assert_called_once_with("test:model", num_ctx=4096)

    def test_num_ctx_in_response(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/load", json={"model": "test:model", "num_ctx": 8192})
        assert r.json()["num_ctx"] == 8192

    def test_num_ctx_below_512_returns_422(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/load", json={"model": "test:model", "num_ctx": 256})
        assert r.status_code == 422

    def test_num_ctx_non_integer_returns_422(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/load", json={"model": "test:model", "num_ctx": "4096"})
        assert r.status_code == 422

    def test_missing_model_field_returns_422(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/load", json={})
        assert r.status_code == 422

    def test_empty_model_name_returns_422(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/load", json={"model": ""})
        assert r.status_code == 422


# ── DELETE /models/{name} ─────────────────────────────────────────────────────

class TestDeleteModel:
    def test_returns_200(self, api: tuple) -> None:
        client, _, _ = api
        r = client.delete("/models/test:model")
        assert r.status_code == 200

    def test_calls_provider_delete_model(self, api: tuple) -> None:
        client, mock_provider, _ = api
        client.delete("/models/test:model")
        mock_provider.delete_model.assert_called_once_with("test:model")

    def test_response_ok_true_on_success(self, api: tuple) -> None:
        client, mock_provider, _ = api
        mock_provider.delete_model.return_value = True
        data = client.delete("/models/test:model").json()
        assert data["ok"] is True


# ── POST /models/pull ─────────────────────────────────────────────────────────

class TestPullModel:
    def test_returns_200(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/models/pull", json={"model": "llama3:latest"})
        assert r.status_code == 200

    def test_missing_model_field_returns_422(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/models/pull", json={})
        assert r.status_code == 422


# ── GET /config ───────────────────────────────────────────────────────────────

class TestGetConfig:
    def test_returns_200(self, api: tuple) -> None:
        client, _, _ = api
        assert client.get("/config").status_code == 200

    def test_returns_threshold_fields(self, api: tuple) -> None:
        client, _, _ = api
        data = client.get("/config").json()
        assert "ram_pause_pct" in data
        assert "cpu_pause_pct" in data
        assert "gpu_vram_pause_pct" in data
        assert "auto_resume_on_ram_pressure" in data


# ── GET /metrics ──────────────────────────────────────────────────────────────

class TestGetMetrics:
    def test_returns_200(self, api: tuple) -> None:
        client, _, _ = api
        assert client.get("/metrics").status_code == 200

    def test_returns_memory_cpu_gpu(self, api: tuple) -> None:
        client, _, _ = api
        data = client.get("/metrics").json()
        assert "memory" in data
        assert "cpu" in data
        assert "gpu" in data


# ── Authentication ────────────────────────────────────────────────────────────

class TestAuth:
    def test_correct_api_key_allowed(self, api: tuple) -> None:
        """_AuthClient injects the correct test key — requests succeed."""
        client, _, _ = api
        r = client.get("/status")
        assert r.status_code == 200

    def test_correct_api_key_on_force_pause_allowed(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/pause/force")
        assert r.status_code == 200


# ── POST /pause/force ─────────────────────────────────────────────────────────

class TestPostForcePause:
    def test_returns_200(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/pause/force")
        assert r.status_code == 200

    def test_calls_provider_force_pause(self, api: tuple) -> None:
        client, mock_provider, _ = api
        client.post("/pause/force")
        mock_provider.force_pause.assert_called_once()

    def test_response_ok_true_on_success(self, api: tuple) -> None:
        client, mock_provider, _ = api
        mock_provider.force_pause.return_value = True
        data = client.post("/pause/force").json()
        assert data["ok"] is True
        assert data["action"] == "force_pause"

    def test_response_ok_false_on_failure(self, api: tuple) -> None:
        client, mock_provider, _ = api
        mock_provider.force_pause.return_value = False
        data = client.post("/pause/force").json()
        assert data["ok"] is False

    def test_watchdog_synced_after_force_pause(self, api: tuple) -> None:
        """Successful force_pause must sync watchdog to PAUSED — same contract as /pause."""
        client, mock_provider, _ = api
        mock_provider.force_pause.return_value = True
        client.post("/pause/force")
        data = client.get("/status").json()
        assert data["watchdog"]["state"] == WatchdogState.PAUSED.value

    def test_watchdog_not_synced_on_failure(self, api: tuple) -> None:
        """If force_pause fails, watchdog must not move to PAUSED."""
        client, mock_provider, _ = api
        mock_provider.force_pause.return_value = False
        client.post("/pause/force")
        data = client.get("/status").json()
        assert data["watchdog"]["state"] == WatchdogState.RUNNING.value


# ── POST /stop/force — non-blocking ──────────────────────────────────────────

class TestPostForceStop:
    def test_returns_200_immediately(self, api: tuple) -> None:
        client, _, _ = api
        r = client.post("/stop/force")
        assert r.status_code == 200

    def test_response_has_ok_and_action(self, api: tuple) -> None:
        client, _, _ = api
        data = client.post("/stop/force").json()
        assert data["ok"] is True
        assert data["action"] == "force_stop"

    def test_wrong_api_key_returns_401(self) -> None:

        """Correct api_key set in settings but wrong key in header → 401."""
        mock_provider  = _make_mock_provider()
        mock_collector = _make_mock_collector()
        settings = _make_test_settings(api_key="secret")

        with (
            patch("llm_valet.api._build_provider",  return_value=mock_provider),
            patch("llm_valet.api._build_collector", return_value=mock_collector),
            patch("llm_valet.api._check_not_root"),
            patch("llm_valet.api._configure_logging"),
            patch("llm_valet.watchdog.psutil.process_iter", return_value=[]),
        ):
            app = create_app(settings)

        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/status", headers={"X-API-Key": "wrong-key"})
            assert r.status_code == 401


class TestSecurityInputValidation:
    """Model name regex and body size limit (E8a, E8b)."""

    @pytest.mark.parametrize("bad_name", [
        "test; echo injected",
        "$(whoami)",
        "model\x00null",
        "../../../etc/passwd",
        "model name with spaces",
        "",
    ])
    def test_load_rejects_invalid_model_name(self, api: tuple, bad_name: str) -> None:
        client, _, _ = api
        r = client.post("/load", json={"model": bad_name})
        assert r.status_code == 422

    @pytest.mark.parametrize("bad_name", [
        "test; echo injected",
        "$(whoami)",
        "../etc/passwd",
    ])
    def test_pull_rejects_invalid_model_name(self, api: tuple, bad_name: str) -> None:
        client, _, _ = api
        r = client.post("/models/pull", json={"model": bad_name})
        assert r.status_code == 422

    @pytest.mark.parametrize("good_name", [
        "llama3.2:3b",
        "qwen3.5:0.8b",
        "mistral:latest",
        "my-model_v2.0",
    ])
    def test_load_accepts_valid_model_name(self, api: tuple, good_name: str) -> None:
        client, _, _ = api
        r = client.post("/load", json={"model": good_name})
        assert r.status_code == 200

    def test_put_config_rejects_oversized_body(self, api: tuple) -> None:
        client, _, _ = api
        big_body = b'{"ram_pause_pct": 85.0, "padding": "' + b"A" * 65536 + b'"}'
        r = client._tc.put(
            "/config",
            content=big_body,
            headers={"Content-Type": "application/json", "X-API-Key": _TEST_API_KEY},
        )
        assert r.status_code == 413
