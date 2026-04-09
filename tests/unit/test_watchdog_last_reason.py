"""Tests for Watchdog.last_reason tracking — pure state machine, no I/O."""
from unittest.mock import AsyncMock, MagicMock

import pytest

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


def _make_watchdog(pause_ok: bool = True, resume_ok: bool = True) -> Watchdog:
    """Build a Watchdog with mock provider and collector."""
    provider = MagicMock()
    provider.pause  = AsyncMock(return_value=pause_ok)
    provider.resume = AsyncMock(return_value=resume_ok)

    metrics = SystemMetrics(
        memory=MemoryMetrics(
            total_mb=16384, used_mb=8192, used_pct=50.0, pressure=PressureLevel.NORMAL
        ),
        cpu=CPUMetrics(used_pct=20.0, core_count=8),
        gpu=GPUMetrics(
            available=False,
            vram_total_mb=None,
            vram_used_mb=None,
            vram_used_pct=None,
            compute_pct=None,
        ),
        disk=DiskMetrics(path="/", total_mb=512000, used_mb=256000, free_mb=256000, used_pct=50.0),
    )
    collector = MagicMock()
    collector.collect = MagicMock(return_value=metrics)

    thresholds = ResourceThresholds(
        ram_pause_pct=85.0,
        ram_resume_pct=60.0,
        cpu_pause_pct=90.0,
        check_interval_seconds=10,
        pause_timeout_seconds=0,  # no grace period — simplifies tests
    )
    return Watchdog(provider, collector, thresholds)


# ── Initial state ──────────────────────────────────────────────────────────────

def test_last_reason_empty_on_init() -> None:
    wd = _make_watchdog()
    assert wd.last_reason == ""


# ── Manual notifications ───────────────────────────────────────────────────────

def test_notify_manual_pause_sets_last_reason() -> None:
    wd = _make_watchdog()
    wd.notify_manual_pause()
    assert wd.last_reason == "manual pause"
    assert wd.state == WatchdogState.PAUSED


def test_notify_manual_resume_sets_last_reason() -> None:
    wd = _make_watchdog()
    wd.notify_manual_pause()
    wd.notify_manual_resume()
    assert wd.last_reason == "manual resume"
    assert wd.state == WatchdogState.RUNNING


# ── Transition on successful pause ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transition_to_paused_sets_last_reason() -> None:
    wd = _make_watchdog(pause_ok=True)
    await wd._transition_to_paused("RAM 90% > 85% threshold")
    assert wd.last_reason == "RAM 90% > 85% threshold"
    assert wd.state == WatchdogState.PAUSED


@pytest.mark.asyncio
async def test_transition_to_paused_fail_does_not_set_last_reason() -> None:
    wd = _make_watchdog(pause_ok=False)
    await wd._transition_to_paused("RAM 90% > 85% threshold")
    # Pause failed — state returns to RUNNING and last_reason is unchanged
    assert wd.last_reason == ""
    assert wd.state == WatchdogState.RUNNING


# ── Transition on successful resume ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_transition_to_running_sets_last_reason() -> None:
    wd = _make_watchdog(pause_ok=True, resume_ok=True)
    await wd._transition_to_paused("test pause")
    await wd._transition_to_running("RAM below 60% resume threshold")
    assert wd.last_reason == "RAM below 60% resume threshold"
    assert wd.state == WatchdogState.RUNNING


@pytest.mark.asyncio
async def test_transition_to_running_fail_preserves_pause_reason() -> None:
    wd = _make_watchdog(pause_ok=True, resume_ok=False)
    await wd._transition_to_paused("RAM spike")
    # Resume fails — state stays PAUSED and last_reason stays from pause
    await wd._transition_to_running("resources cleared")
    assert wd.last_reason == "RAM spike"
    assert wd.state == WatchdogState.PAUSED
