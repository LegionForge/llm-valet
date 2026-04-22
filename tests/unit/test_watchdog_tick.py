"""
Watchdog._tick() integration tests — state machine driven by mocked metrics
and mocked game detection.  No real psutil, no real provider.

These tests call _tick() directly rather than run() to avoid the sleep loop.
_detect_game is patched at the module level so psutil is never touched.
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _metrics(
    ram_pct: float = 50.0,
    cpu_pct: float = 20.0,
    total_mb: int = 16384,
) -> SystemMetrics:
    return SystemMetrics(
        memory=MemoryMetrics(
            total_mb=total_mb,
            used_mb=int(total_mb * ram_pct / 100),
            used_pct=ram_pct,
            pressure=PressureLevel.NORMAL,
        ),
        cpu=CPUMetrics(used_pct=cpu_pct, core_count=8),
        gpu=GPUMetrics(
            available=False,
            vram_total_mb=None,
            vram_used_mb=None,
            vram_used_pct=None,
            compute_pct=None,
        ),
        disk=DiskMetrics(path="/", total_mb=512000, used_mb=128000, free_mb=384000, used_pct=25.0),
    )


def _watchdog(
    ram_pct: float = 50.0,
    cpu_pct: float = 20.0,
    pause_ok: bool = True,
    resume_ok: bool = True,
    pause_timeout: int = 0,
    cpu_sustained_seconds: int = 0,
    auto_resume_on_ram: bool = True,
    health_ok: bool = True,
) -> Watchdog:
    provider = MagicMock()
    provider.pause        = AsyncMock(return_value=pause_ok)
    provider.resume       = AsyncMock(return_value=resume_ok)
    provider.health_check = AsyncMock(return_value=health_ok)

    collector = MagicMock()
    collector.collect = MagicMock(return_value=_metrics(ram_pct=ram_pct, cpu_pct=cpu_pct))

    thresholds = ResourceThresholds(
        ram_pause_pct=85.0,
        ram_resume_pct=60.0,
        cpu_pause_pct=90.0,
        cpu_sustained_seconds=cpu_sustained_seconds,
        check_interval_seconds=10,
        pause_timeout_seconds=pause_timeout,
        auto_resume_on_ram_pressure=auto_resume_on_ram,
    )
    return Watchdog(provider, collector, thresholds)


# ── No-op tick (normal conditions) ───────────────────────────────────────────

class TestTickNoOp:
    async def test_normal_conditions_stay_running(self) -> None:
        wd = _watchdog(ram_pct=50.0, cpu_pct=20.0)
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.RUNNING
        wd._provider.pause.assert_not_called()

    async def test_tick_calls_collector_each_time(self) -> None:
        wd = _watchdog()
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
            await wd._tick()
        assert wd._collector.collect.call_count == 2


# ── Game detection ────────────────────────────────────────────────────────────

class TestGameDetection:
    async def test_game_detected_triggers_pause(self) -> None:
        wd = _watchdog(ram_pct=50.0)
        with patch(
            "llm_valet.watchdog._detect_game",
            return_value=(True, "game detected — steamapps/common/Hades"),
        ):
            await wd._tick()
        assert wd.state == WatchdogState.PAUSED
        wd._provider.pause.assert_called_once()

    async def test_game_detected_sets_pause_trigger(self) -> None:
        wd = _watchdog(ram_pct=50.0)
        with patch(
            "llm_valet.watchdog._detect_game",
            return_value=(True, "game detected — steamapps/common/Hades"),
        ):
            await wd._tick()
        assert wd._pause_trigger == "game"

    async def test_game_detected_sets_last_reason(self) -> None:
        wd = _watchdog(ram_pct=50.0)
        with patch(
            "llm_valet.watchdog._detect_game",
            return_value=(True, "game detected — steamapps/common/Hades"),
        ):
            await wd._tick()
        assert "game detected" in wd.last_reason

    async def test_no_game_no_pressure_stays_running(self) -> None:
        wd = _watchdog(ram_pct=50.0)
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.RUNNING


# ── RAM pressure ──────────────────────────────────────────────────────────────

class TestRamPressure:
    async def test_ram_above_threshold_triggers_pause(self) -> None:
        wd = _watchdog(ram_pct=90.0)  # 90% > 85% threshold
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.PAUSED
        assert wd._pause_trigger == "ram"

    async def test_ram_below_threshold_no_pause(self) -> None:
        wd = _watchdog(ram_pct=80.0)  # 80% < 85% threshold
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.RUNNING

    async def test_pause_fail_stays_running(self) -> None:
        wd = _watchdog(ram_pct=90.0, pause_ok=False)
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.RUNNING


# ── CPU sustained ticks ───────────────────────────────────────────────────────

class TestCpuSustained:
    async def test_cpu_spike_alone_does_not_pause(self) -> None:
        """cpu_sustained_seconds=30 requires 3 ticks at interval=10 before pausing."""
        wd = _watchdog(cpu_pct=95.0, cpu_sustained_seconds=30)
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()   # tick 1 — 10s elapsed, need 30s
        assert wd.state == WatchdogState.RUNNING

    async def test_cpu_sustained_over_threshold_triggers_pause(self) -> None:
        """After enough ticks to reach cpu_sustained_seconds, pause fires."""
        wd = _watchdog(cpu_pct=95.0, cpu_sustained_seconds=10)
        # cpu_sustained_seconds=10, check_interval=10 → 1 tick is enough
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.PAUSED
        assert wd._pause_trigger == "cpu"

    async def test_cpu_ticks_reset_when_pressure_clears(self) -> None:
        wd = _watchdog(cpu_pct=50.0, cpu_sustained_seconds=30)
        wd._cpu_pressure_ticks = 2  # as if 2 ticks already elapsed
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()   # cpu_pct=50 — below threshold — resets counter
        assert wd._cpu_pressure_ticks == 0


# ── Auto-resume after grace period ───────────────────────────────────────────

class TestAutoResume:
    async def test_paused_state_resumes_after_grace(self) -> None:
        """With pause_timeout=0 and low RAM, PAUSED should transition to RUNNING on next tick."""
        wd = _watchdog(ram_pct=50.0, pause_timeout=0)
        # Force into PAUSED state from a RAM trigger so gate doesn't block
        wd._pause_trigger = "game"
        wd._state = WatchdogState.PAUSED
        wd._paused_at = 0.0  # epoch — elapsed >> 0s grace

        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()

        assert wd.state == WatchdogState.RUNNING
        wd._provider.resume.assert_called_once()

    async def test_paused_state_waits_for_grace_period(self) -> None:
        """With a long grace period, PAUSED should not resume on the next tick."""
        import time
        wd = _watchdog(ram_pct=50.0, pause_timeout=3600)
        wd._pause_trigger = "game"
        wd._state = WatchdogState.PAUSED
        wd._paused_at = time.monotonic()  # just paused — 3600s remaining

        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()

        assert wd.state == WatchdogState.PAUSED
        wd._provider.resume.assert_not_called()

    async def test_ram_pause_blocks_auto_resume(self) -> None:
        """auto_resume_on_ram_pressure=False: RAM-triggered pauses require manual /resume."""
        wd = _watchdog(ram_pct=50.0, pause_timeout=0, auto_resume_on_ram=False)
        wd._pause_trigger = "ram"
        wd._state = WatchdogState.PAUSED
        wd._paused_at = 0.0

        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()

        assert wd.state == WatchdogState.PAUSED
        wd._provider.resume.assert_not_called()


# ── Full RUNNING → PAUSED → RUNNING cycle ────────────────────────────────────

class TestFullCycle:
    async def test_game_start_pause_game_end_resume(self) -> None:
        wd = _watchdog(ram_pct=50.0, pause_timeout=0)

        # Tick 1: game detected → PAUSED
        with patch(
            "llm_valet.watchdog._detect_game",
            return_value=(True, "game detected — steamapps/common/Hades"),
        ):
            await wd._tick()
        assert wd.state == WatchdogState.PAUSED

        # Tick 2: game gone, grace=0, low RAM → RUNNING
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.RUNNING

        wd._provider.pause.assert_called_once()
        wd._provider.resume.assert_called_once()


# ── Provider-down detection (Gap 3) ──────────────────────────────────────────

class TestProviderDown:
    async def test_running_provider_crash_transitions_to_provider_down(self) -> None:
        wd = _watchdog(health_ok=False)
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.PROVIDER_DOWN
        assert "unreachable" in wd.last_reason
        wd._provider.pause.assert_not_called()

    async def test_provider_down_recovers_to_running(self) -> None:
        wd = _watchdog(health_ok=True)
        wd._state = WatchdogState.PROVIDER_DOWN
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.RUNNING
        assert "recovered" in wd.last_reason

    async def test_provider_down_stays_down_when_still_unhealthy(self) -> None:
        wd = _watchdog(health_ok=False)
        wd._state = WatchdogState.PROVIDER_DOWN
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.PROVIDER_DOWN
        wd._provider.pause.assert_not_called()

    async def test_paused_provider_crash_transitions_to_provider_down(self) -> None:
        wd = _watchdog(health_ok=False)
        wd._state = WatchdogState.PAUSED
        wd._paused_at = time.monotonic()
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.PROVIDER_DOWN

    async def test_health_check_exception_treated_as_down(self) -> None:
        wd = _watchdog()
        wd._provider.health_check = AsyncMock(side_effect=Exception("conn refused"))
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.PROVIDER_DOWN

    async def test_provider_down_recover_exception_stays_down(self) -> None:
        wd = _watchdog()
        wd._state = WatchdogState.PROVIDER_DOWN
        wd._provider.health_check = AsyncMock(side_effect=Exception("conn refused"))
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        assert wd.state == WatchdogState.PROVIDER_DOWN

    async def test_recovered_provider_immediately_evaluates_resources(self) -> None:
        """On recovery tick, resource evaluation fires — high RAM should pause."""
        wd = _watchdog(ram_pct=90.0, health_ok=True)  # RAM above 85% threshold
        wd._state = WatchdogState.PROVIDER_DOWN
        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()
        # Recovered + high RAM → should immediately pause
        assert wd.state == WatchdogState.PAUSED


# ── Transition failure paths ──────────────────────────────────────────────────

class TestTransitionFailures:
    async def test_resume_fail_stays_paused(self) -> None:
        """If provider.resume() fails, state rolls back to PAUSED — not stuck at RESUMING."""
        wd = _watchdog(ram_pct=50.0, resume_ok=False, pause_timeout=0)
        wd._pause_trigger = "game"
        wd._state = WatchdogState.PAUSED
        wd._paused_at = 0.0  # grace already elapsed

        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()

        assert wd.state == WatchdogState.PAUSED
        wd._provider.resume.assert_called_once()

    async def test_paused_while_game_still_running_is_noop(self) -> None:
        """PAUSED + game still active: no pause() or resume() call — tick is a pure no-op."""
        wd = _watchdog(ram_pct=50.0, pause_timeout=3600)
        wd._pause_trigger = "game"
        wd._state = WatchdogState.PAUSED
        wd._paused_at = time.monotonic()

        with patch(
            "llm_valet.watchdog._detect_game",
            return_value=(True, "game detected — steamapps/common/Hades"),
        ):
            await wd._tick()

        assert wd.state == WatchdogState.PAUSED
        wd._provider.pause.assert_not_called()
        wd._provider.resume.assert_not_called()

    async def test_hysteresis_zone_stays_paused(self) -> None:
        """RAM between ram_resume_pct and ram_pause_pct — safe_to_resume is False, no resume."""
        # RAM=70% is above resume threshold (60%) but below pause threshold (85%)
        wd = _watchdog(ram_pct=70.0, pause_timeout=0)
        wd._pause_trigger = "game"
        wd._state = WatchdogState.PAUSED
        wd._paused_at = 0.0  # grace already elapsed

        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()

        assert wd.state == WatchdogState.PAUSED
        wd._provider.resume.assert_not_called()


# ── GPU pressure ──────────────────────────────────────────────────────────────

def _metrics_with_gpu(vram_pct: float = 50.0) -> SystemMetrics:
    from llm_valet.resources.base import DiskMetrics
    return SystemMetrics(
        memory=MemoryMetrics(
            total_mb=16384,
            used_mb=8192,
            used_pct=50.0,
            pressure=PressureLevel.NORMAL,
        ),
        cpu=CPUMetrics(used_pct=20.0, core_count=8),
        gpu=GPUMetrics(
            available=True,
            vram_total_mb=8192,
            vram_used_mb=int(8192 * vram_pct / 100),
            vram_used_pct=vram_pct,
            compute_pct=50.0,
        ),
        disk=DiskMetrics(path="/", total_mb=512000, used_mb=128000, free_mb=384000, used_pct=25.0),
    )


class TestGpuPressure:
    async def test_gpu_vram_above_threshold_triggers_pause(self) -> None:
        wd = _watchdog()
        wd._collector.collect = MagicMock(return_value=_metrics_with_gpu(vram_pct=90.0))

        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()

        assert wd.state == WatchdogState.PAUSED
        assert wd._pause_trigger == "gpu"

    async def test_gpu_vram_below_threshold_no_pause(self) -> None:
        wd = _watchdog()
        wd._collector.collect = MagicMock(return_value=_metrics_with_gpu(vram_pct=70.0))

        with patch("llm_valet.watchdog._detect_game", return_value=(False, "")):
            await wd._tick()

        assert wd.state == WatchdogState.RUNNING
