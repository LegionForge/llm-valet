"""
OllamaProvider integration tests — direct API calls to a live Ollama instance.

Run: pytest -m integration tests/integration/test_ollama_provider.py
"""

from __future__ import annotations

import asyncio

import pytest

from llm_valet.providers.ollama import OllamaProvider
from tests.conftest import _EVICTION_SETTLE_S

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("ollama_url")]


@pytest.fixture()
async def provider(test_model: str) -> OllamaProvider:
    """Fresh OllamaProvider configured with the test model."""
    return OllamaProvider(model_name=test_model, request_timeout=30.0)


@pytest.fixture()
async def loaded_provider(provider: OllamaProvider, test_model: str):
    """
    Provider with model loaded and confirmed present in /api/ps.

    load_model() (keep_alive=-1) returns before /api/ps reflects the loaded
    state. We poll until the model appears so tests don't race with that lag.
    """
    await provider.load_model(test_model)
    for _ in range(20):
        if (await provider.status()).model_loaded:
            break
        await asyncio.sleep(0.25)
    yield provider
    await provider.pause()


# ── Health & status ───────────────────────────────────────────────────────────


class TestHealthAndStatus:
    async def test_health_check(self, provider: OllamaProvider) -> None:
        assert await provider.health_check() is True

    async def test_status_running(self, provider: OllamaProvider) -> None:
        status = await provider.status()
        assert status.running is True

    async def test_status_no_model_loaded(self, provider: OllamaProvider) -> None:
        await provider.pause()  # ensure clean slate
        status = await provider.status()
        assert status.model_loaded is False
        assert status.model_name is None
        assert status.memory_used_mb is None


# ── Model listing ─────────────────────────────────────────────────────────────


class TestModelList:
    async def test_list_models_includes_test_model(
        self, test_model: str, provider: OllamaProvider
    ) -> None:
        models = await provider.list_models()
        assert any(test_model in m.name for m in models)

    async def test_list_models_returns_size(
        self, test_model: str, provider: OllamaProvider
    ) -> None:
        models = await provider.list_models()
        m = next(m for m in models if test_model in m.name)
        assert m.size_mb > 0


# ── Load / pause / resume ─────────────────────────────────────────────────────


class TestLoadPauseResume:
    async def test_load_model_succeeds(self, test_model: str, provider: OllamaProvider) -> None:
        assert await provider.load_model(test_model) is True
        status = await provider.status()
        assert status.model_loaded is True
        assert status.model_name is not None
        assert test_model in status.model_name
        await provider.pause()

    async def test_load_model_with_custom_ctx(
        self, test_model: str, provider: OllamaProvider
    ) -> None:
        assert await provider.load_model(test_model, num_ctx=2048) is True
        status = await provider.status()
        assert status.model_loaded is True
        await provider.pause()

    async def test_pause_unloads_model(self, loaded_provider: OllamaProvider) -> None:
        assert await loaded_provider.pause() is True
        await asyncio.sleep(_EVICTION_SETTLE_S)
        status = await loaded_provider.status()
        assert status.model_loaded is False

    async def test_pause_when_already_unloaded_is_idempotent(
        self, provider: OllamaProvider
    ) -> None:
        await provider.pause()  # ensure unloaded
        assert await provider.pause() is True

    async def test_resume_reloads_model(self, loaded_provider: OllamaProvider) -> None:
        await loaded_provider.pause()
        assert await loaded_provider.resume() is True
        status = await loaded_provider.status()
        assert status.model_loaded is True
        await loaded_provider.pause()

    async def test_invalid_model_name_rejected(self, provider: OllamaProvider) -> None:
        assert await provider.load_model("../../../etc/passwd") is False
        assert await provider.load_model("model with spaces") is False


# ── Force pause ───────────────────────────────────────────────────────────────


class TestForcePause:
    async def test_force_pause_unloads_model(self, loaded_provider: OllamaProvider) -> None:
        # force_pause() kills runner processes if present, else falls back to pause().
        # Either path must result in the model being evicted.
        assert await loaded_provider.force_pause() is True
        await asyncio.sleep(_EVICTION_SETTLE_S)
        status = await loaded_provider.status()
        assert status.model_loaded is False

    async def test_force_pause_when_already_unloaded(self, provider: OllamaProvider) -> None:
        await provider.pause()  # ensure nothing loaded
        assert await provider.force_pause() is True  # fallback to pause() returns True
