"""
Watchdog integration tests — real OllamaProvider, mock ResourceCollector.

The mock collector injects precise resource metrics (e.g. high RAM to trigger
auto-pause) without needing to stress the machine. The real provider executes
actual Ollama API calls so FSM transitions exercise the full pause/resume path.

Run: pytest -m integration tests/integration/test_watchdog_live.py
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from llm_valet.providers.ollama import OllamaProvider
from llm_valet.resources.base import (
    CPUMetrics,
    DiskMetrics,
    GPUMetrics,
    MemoryMetrics,
    PressureLevel,
    ResourceThresholds,
    SystemMetrics,
)
from llm_valet.watchdog import Watchdog, WatchdogState

# Ollama's /api/ps lags ~1 s after keep_alive=0 eviction -- set expires_at to
# near-future rather than clearing immediately.  Tests must wait before asserting
# model_loaded==False via provider.status().
_EVICTION_SETTLE_S = 1.5

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("ollama_url")]

_THRESHOLDS = ResourceThresholds(
    ram_pause_pct=85.0,
    ram_resume_pct=60.0,
    cpu_pause_pct=90.0,
    cpu_sustained_seconds=30,
    pause_timeout_seconds=0,  # no grace period — resumes immediately when safe
    check_interval_seconds=300,
)


def _metrics(ram_pct: float = 50.0) -> SystemMetrics:
    """Build SystemMetrics with a controllable RAM percentage."""
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


@pytest.fixture()
async def components(test_model: str):
    """
    Yields (provider, mock_collector, watchdog).
    Provider has the test model configured. Collector is a MagicMock returning
    safe metrics by default. Model is unloaded after each test.
    """
    provider = OllamaProvider(model_name=test_model, request_timeout=30.0)
    collector = MagicMock()
    collector.collect.return_value = _metrics(ram_pct=50.0)
    wd = Watchdog(provider, collector, _THRESHOLDS)
    yield provider, collector, wd
    await provider.pause()  # ensure model unloaded regardless of test outcome


# ── Start / stop ──────────────────────────────────────────────────────────────


class TestStartStop:
    async def test_initial_state_is_running(self, components) -> None:  # type: ignore[no-untyped-def]
        _, _, wd = components
        assert wd.state == WatchdogState.RUNNING

    async def test_run_and_stop_cleanly(self, components) -> None:  # type: ignore[no-untyped-def]
        _, _, wd = components
        task = asyncio.create_task(wd.run())
        await asyncio.sleep(0.05)
        await wd.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert not wd._running


# ── Manual state sync ─────────────────────────────────────────────────────────


class TestManualSync:
    async def test_notify_manual_pause(self, components) -> None:  # type: ignore[no-untyped-def]
        _, _, wd = components
        wd.notify_manual_pause()
        assert wd.state == WatchdogState.PAUSED
        assert wd.last_reason == "manual pause"
        assert wd._paused_at is not None

    async def test_notify_manual_resume(self, components) -> None:  # type: ignore[no-untyped-def]
        _, _, wd = components
        wd.notify_manual_pause()
        wd.notify_manual_resume()
        assert wd.state == WatchdogState.RUNNING
        assert wd.last_reason == "manual resume"
        assert wd._pause_trigger == ""
        assert wd._paused_at is None


# ── Resource-triggered transitions ───────────────────────────────────────────


class TestResourceTriggeredTransitions:
    async def test_high_ram_triggers_pause(  # type: ignore[no-untyped-def]
        self, test_model: str, components
    ) -> None:
        provider, collector, wd = components
        await provider.load_model(test_model)
        collector.collect.return_value = _metrics(ram_pct=90.0)  # above 85% threshold
        await wd._tick()
        assert wd.state == WatchdogState.PAUSED
        assert wd._pause_trigger == "ram"
        await asyncio.sleep(_EVICTION_SETTLE_S)
        assert (await provider.status()).model_loaded is False

    async def test_low_ram_resumes_when_grace_expires(  # type: ignore[no-untyped-def]
        self, test_model: str, components
    ) -> None:
        provider, collector, wd = components
        await provider.load_model(test_model)
        collector.collect.return_value = _metrics(ram_pct=90.0)
        await wd._tick()
        assert wd.state == WatchdogState.PAUSED
        # Drop below resume threshold; pause_timeout_seconds=0 so grace is already elapsed
        collector.collect.return_value = _metrics(ram_pct=40.0)
        await wd._tick()
        assert wd.state == WatchdogState.RUNNING
        assert (await provider.status()).model_loaded is True
        await provider.pause()

    async def test_hysteresis_band_stays_paused(  # type: ignore[no-untyped-def]
        self, test_model: str, components
    ) -> None:
        provider, collector, wd = components
        await provider.load_model(test_model)
        collector.collect.return_value = _metrics(ram_pct=90.0)
        await wd._tick()
        assert wd.state == WatchdogState.PAUSED
        # RAM in hysteresis band (60%-85%): above ram_resume_pct, should not resume
        collector.collect.return_value = _metrics(ram_pct=70.0)
        await wd._tick()
        assert wd.state == WatchdogState.PAUSED


# ── Provider-down detection ───────────────────────────────────────────────────


class TestProviderDown:
    async def test_provider_down_detected_then_recovered(  # type: ignore[no-untyped-def]
        self, components
    ) -> None:
        provider, _, wd = components
        original = provider.health_check
        call_count = 0

        async def flaky_health_check() -> bool:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return False  # tick 1: provider appears down
            return await original()

        provider.health_check = flaky_health_check  # type: ignore[method-assign]
        await wd._tick()
        assert wd.state == WatchdogState.PROVIDER_DOWN

        await wd._tick()  # tick 2: provider recovers
        assert wd.state == WatchdogState.RUNNING

        provider.health_check = original  # type: ignore[method-assign]
