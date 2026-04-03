import enum
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


class PressureLevel(enum.Enum):
    NORMAL = "normal"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass
class MemoryMetrics:
    total_mb: int
    used_mb: int
    used_pct: float
    pressure: PressureLevel  # from OS pressure API where available


@dataclass
class CPUMetrics:
    used_pct: float   # 1-second average
    core_count: int


@dataclass
class GPUMetrics:
    available: bool              # False if no GPU driver accessible
    vram_total_mb: Optional[int]
    vram_used_mb: Optional[int]
    vram_used_pct: Optional[float]
    compute_pct: Optional[float]


@dataclass
class SystemMetrics:
    memory: MemoryMetrics
    cpu: CPUMetrics
    gpu: GPUMetrics
    timestamp: float = field(default_factory=time.time)


class ResourceCollector(ABC):
    @abstractmethod
    def collect(self) -> SystemMetrics: ...

    @abstractmethod
    def supported_metrics(self) -> set[str]: ...
    # e.g. {"memory", "cpu", "gpu", "pressure"}
    # Callers check this before trusting Optional fields


@dataclass
class ResourceThresholds:
    ram_pause_pct: float = 85.0
    ram_resume_pct: float = 60.0      # hysteresis gap prevents oscillation
    cpu_pause_pct: float = 90.0
    cpu_sustained_seconds: int = 30   # must exceed threshold for this long before acting
    gpu_vram_pause_pct: float = 85.0
    pause_timeout_seconds: int = 120  # grace period before resume after resource clears
    check_interval_seconds: int = 10


class ThresholdEngine:
    """
    Pure logic — no I/O. Accepts metrics + thresholds, returns a pause decision.

    The caller is responsible for tracking sustained-seconds externally:
    increment a counter each check_interval while evaluate() returns True,
    and only act when counter * check_interval >= cpu_sustained_seconds.
    RAM and GPU pressure trigger immediately (no sustained window).
    """

    def __init__(self, thresholds: ResourceThresholds) -> None:
        self._t = thresholds

    def evaluate(self, metrics: SystemMetrics) -> tuple[bool, str]:
        """
        Returns (should_pause, reason).

        - RAM: pause immediately when used_pct >= ram_pause_pct OR
               pressure == CRITICAL.
        - CPU: caller enforces the sustained window; this returns True whenever
               cpu exceeds the threshold so the caller can count ticks.
        - GPU VRAM: pause immediately when vram_used_pct >= gpu_vram_pause_pct.
        - Resume: returns False only when ALL metrics are below their resume thresholds.
          The resume check is separated into evaluate_resume() so callers can
          apply the grace-period window independently.
        """
        t = self._t
        mem = metrics.memory
        cpu = metrics.cpu
        gpu = metrics.gpu

        # Memory pressure — OS-level signal takes priority on macOS
        if mem.pressure == PressureLevel.CRITICAL:
            return True, "memory pressure level CRITICAL"

        if mem.used_pct >= t.ram_pause_pct:
            return True, f"RAM {mem.used_pct:.1f}% >= {t.ram_pause_pct}% threshold"

        # CPU (caller accumulates ticks; we just report the instantaneous check)
        if cpu.used_pct >= t.cpu_pause_pct:
            return True, f"CPU {cpu.used_pct:.1f}% >= {t.cpu_pause_pct}% threshold"

        # GPU VRAM
        if gpu.available and gpu.vram_used_pct is not None:
            if gpu.vram_used_pct >= t.gpu_vram_pause_pct:
                return True, f"GPU VRAM {gpu.vram_used_pct:.1f}% >= {t.gpu_vram_pause_pct}% threshold"

        return False, ""

    def evaluate_resume(self, metrics: SystemMetrics) -> tuple[bool, str]:
        """
        Returns (safe_to_resume, reason).

        All metrics must be below their resume thresholds before this returns True.
        Hysteresis: RAM resumes at ram_resume_pct, not ram_pause_pct.
        CPU and GPU resume at their pause threshold (no separate config needed
        since the watchdog's grace period provides the sustained buffer on resume).
        """
        t = self._t
        mem = metrics.memory
        cpu = metrics.cpu
        gpu = metrics.gpu

        if mem.pressure == PressureLevel.CRITICAL:
            return False, "memory pressure level still CRITICAL"

        if mem.used_pct >= t.ram_resume_pct:
            return False, f"RAM {mem.used_pct:.1f}% >= resume threshold {t.ram_resume_pct}%"

        if cpu.used_pct >= t.cpu_pause_pct:
            return False, f"CPU {cpu.used_pct:.1f}% still elevated"

        if gpu.available and gpu.vram_used_pct is not None:
            if gpu.vram_used_pct >= t.gpu_vram_pause_pct:
                return False, f"GPU VRAM {gpu.vram_used_pct:.1f}% still elevated"

        return True, "all resource metrics below resume thresholds"
