"""Tests for ThresholdEngine — pure logic, no I/O required."""

from llm_valet.resources.base import (
    CPUMetrics,
    DiskMetrics,
    GPUMetrics,
    MemoryMetrics,
    PressureLevel,
    ResourceThresholds,
    SystemMetrics,
    ThresholdEngine,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _metrics(
    ram_pct: float = 50.0,
    cpu_pct: float = 30.0,
    pressure: PressureLevel = PressureLevel.NORMAL,
    gpu_available: bool = False,
    vram_pct: float | None = None,
) -> SystemMetrics:
    """Build a SystemMetrics with sensible defaults for testing."""
    return SystemMetrics(
        memory=MemoryMetrics(
            total_mb=16384,
            used_mb=int(16384 * ram_pct / 100),
            used_pct=ram_pct,
            pressure=pressure,
        ),
        cpu=CPUMetrics(used_pct=cpu_pct, core_count=8),
        gpu=GPUMetrics(
            available=gpu_available,
            vram_total_mb=8192 if gpu_available else None,
            vram_used_mb=int(8192 * (vram_pct or 0) / 100) if gpu_available else None,
            vram_used_pct=vram_pct,
            compute_pct=None,
        ),
        disk=DiskMetrics(path="/", total_mb=512000, used_mb=256000, free_mb=256000, used_pct=50.0),
    )


DEFAULT_THRESHOLDS = ResourceThresholds(
    ram_pause_pct=85.0,
    ram_resume_pct=60.0,
    cpu_pause_pct=90.0,
    gpu_vram_pause_pct=85.0,
)


# ── evaluate() — pause decisions ─────────────────────────────────────────────


class TestEvaluatePause:
    def setup_method(self) -> None:
        self.engine = ThresholdEngine(DEFAULT_THRESHOLDS)

    def test_all_normal_no_pause(self) -> None:
        should_pause, reason = self.engine.evaluate(_metrics())
        assert should_pause is False
        assert reason == ""

    def test_ram_at_threshold_triggers_pause(self) -> None:
        should_pause, reason = self.engine.evaluate(_metrics(ram_pct=85.0))
        assert should_pause is True
        assert "RAM" in reason
        assert "85.0%" in reason

    def test_ram_below_threshold_no_pause(self) -> None:
        should_pause, _ = self.engine.evaluate(_metrics(ram_pct=84.9))
        assert should_pause is False

    def test_ram_above_threshold_triggers_pause(self) -> None:
        should_pause, reason = self.engine.evaluate(_metrics(ram_pct=95.0))
        assert should_pause is True
        assert "RAM" in reason

    def test_cpu_at_threshold_triggers_pause(self) -> None:
        should_pause, reason = self.engine.evaluate(_metrics(cpu_pct=90.0))
        assert should_pause is True
        assert "CPU" in reason

    def test_cpu_below_threshold_no_pause(self) -> None:
        should_pause, _ = self.engine.evaluate(_metrics(cpu_pct=89.9))
        assert should_pause is False

    def test_critical_memory_pressure_informational_only(self) -> None:
        """CRITICAL pressure alone must not trigger pause — RAM% is authoritative.
        Removed as hard trigger in v0.1.0: transient CRITICAL during model load on
        Apple Silicon was defeating user-configured ram_pause_pct thresholds."""
        should_pause, _ = self.engine.evaluate(
            _metrics(ram_pct=10.0, pressure=PressureLevel.CRITICAL)
        )
        assert should_pause is False

    def test_warn_memory_pressure_alone_does_not_pause(self) -> None:
        """WARN pressure without crossing RAM % threshold should not pause."""
        should_pause, _ = self.engine.evaluate(_metrics(ram_pct=50.0, pressure=PressureLevel.WARN))
        assert should_pause is False

    def test_critical_pressure_does_not_override_ram_pct(self) -> None:
        """OS-level CRITICAL does not override RAM% — user threshold is authoritative."""
        should_pause, _ = self.engine.evaluate(
            _metrics(ram_pct=40.0, pressure=PressureLevel.CRITICAL)
        )
        assert should_pause is False

    def test_gpu_vram_at_threshold_triggers_pause(self) -> None:
        should_pause, reason = self.engine.evaluate(_metrics(gpu_available=True, vram_pct=85.0))
        assert should_pause is True
        assert "GPU VRAM" in reason

    def test_gpu_vram_below_threshold_no_pause(self) -> None:
        should_pause, _ = self.engine.evaluate(_metrics(gpu_available=True, vram_pct=84.9))
        assert should_pause is False

    def test_gpu_unavailable_vram_ignored(self) -> None:
        """When gpu.available is False, vram_used_pct must not trigger pause."""
        should_pause, _ = self.engine.evaluate(_metrics(gpu_available=False, vram_pct=99.0))
        assert should_pause is False

    def test_reason_string_is_nonempty_on_pause(self) -> None:
        should_pause, reason = self.engine.evaluate(_metrics(ram_pct=90.0))
        assert should_pause is True
        assert len(reason) > 0

    def test_reason_string_is_empty_on_no_pause(self) -> None:
        should_pause, reason = self.engine.evaluate(_metrics())
        assert should_pause is False
        assert reason == ""


# ── evaluate_resume() — resume decisions ──────────────────────────────────────


class TestEvaluateResume:
    def setup_method(self) -> None:
        self.engine = ThresholdEngine(DEFAULT_THRESHOLDS)

    def test_all_clear_safe_to_resume(self) -> None:
        safe, reason = self.engine.evaluate_resume(_metrics())
        assert safe is True
        assert "below" in reason

    def test_ram_above_resume_threshold_blocks_resume(self) -> None:
        safe, reason = self.engine.evaluate_resume(_metrics(ram_pct=65.0))
        assert safe is False
        assert "RAM" in reason

    def test_ram_at_resume_threshold_blocks_resume(self) -> None:
        """Boundary: exactly at threshold should block (>=)."""
        safe, _ = self.engine.evaluate_resume(_metrics(ram_pct=60.0))
        assert safe is False

    def test_ram_just_below_resume_threshold_allows_resume(self) -> None:
        safe, _ = self.engine.evaluate_resume(_metrics(ram_pct=59.9))
        assert safe is True

    def test_hysteresis_gap_prevents_thrashing(self) -> None:
        """
        A value between ram_resume_pct (60) and ram_pause_pct (85) should:
          - NOT trigger pause (evaluate returns False)
          - NOT allow resume (evaluate_resume returns False)
        This is the hysteresis band that prevents rapid pause/resume cycling.
        """
        mid = 70.0
        should_pause, _ = self.engine.evaluate(_metrics(ram_pct=mid))
        safe_to_resume, _ = self.engine.evaluate_resume(_metrics(ram_pct=mid))
        assert should_pause is False
        assert safe_to_resume is False

    def test_critical_pressure_does_not_block_resume(self) -> None:
        """CRITICAL pressure is informational only — RAM% governs resume decisions."""
        safe, _ = self.engine.evaluate_resume(
            _metrics(ram_pct=10.0, pressure=PressureLevel.CRITICAL)
        )
        assert safe is True

    def test_elevated_cpu_blocks_resume(self) -> None:
        safe, reason = self.engine.evaluate_resume(_metrics(cpu_pct=95.0))
        assert safe is False
        assert "CPU" in reason

    def test_elevated_gpu_vram_blocks_resume(self) -> None:
        safe, reason = self.engine.evaluate_resume(_metrics(gpu_available=True, vram_pct=90.0))
        assert safe is False
        assert "GPU VRAM" in reason

    def test_gpu_unavailable_does_not_block_resume(self) -> None:
        safe, _ = self.engine.evaluate_resume(_metrics(gpu_available=False, vram_pct=99.0))
        assert safe is True


# ── Custom threshold values ────────────────────────────────────────────────────


class TestCustomThresholds:
    def test_custom_ram_pause_threshold(self) -> None:
        engine = ThresholdEngine(ResourceThresholds(ram_pause_pct=70.0))
        should_pause, _ = engine.evaluate(_metrics(ram_pct=70.0))
        assert should_pause is True

    def test_custom_ram_below_custom_threshold_no_pause(self) -> None:
        engine = ThresholdEngine(ResourceThresholds(ram_pause_pct=70.0))
        should_pause, _ = engine.evaluate(_metrics(ram_pct=69.9))
        assert should_pause is False

    def test_custom_cpu_threshold(self) -> None:
        engine = ThresholdEngine(ResourceThresholds(cpu_pause_pct=50.0))
        should_pause, _ = engine.evaluate(_metrics(cpu_pct=50.0))
        assert should_pause is True

    def test_custom_gpu_vram_threshold(self) -> None:
        engine = ThresholdEngine(ResourceThresholds(gpu_vram_pause_pct=70.0))
        should_pause, _ = engine.evaluate(_metrics(gpu_available=True, vram_pct=70.0))
        assert should_pause is True

    def test_tight_hysteresis_gap(self) -> None:
        """Narrow pause/resume gap — still must not cause simultaneous pause+resume."""
        engine = ThresholdEngine(ResourceThresholds(ram_pause_pct=80.0, ram_resume_pct=79.0))
        mid = 79.5
        should_pause, _ = engine.evaluate(_metrics(ram_pct=mid))
        safe_to_resume, _ = engine.evaluate_resume(_metrics(ram_pct=mid))
        assert should_pause is False
        assert safe_to_resume is False
