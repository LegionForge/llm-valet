import enum
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import psutil


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
    used_pct: float  # 1-second average
    core_count: int


@dataclass
class GPUMetrics:
    available: bool  # False if no GPU driver accessible
    vram_total_mb: int | None
    vram_used_mb: int | None
    vram_used_pct: float | None
    compute_pct: float | None


@dataclass
class DiskMetrics:
    path: str  # monitored mount point ("/" or "C:\\")
    total_mb: int
    used_mb: int
    free_mb: int
    used_pct: float


@dataclass
class SystemMetrics:
    memory: MemoryMetrics
    cpu: CPUMetrics
    gpu: GPUMetrics
    disk: DiskMetrics
    timestamp: float = field(default_factory=time.time)


class ResourceCollector(ABC):
    @abstractmethod
    def collect(self) -> SystemMetrics: ...

    @abstractmethod
    def supported_metrics(self) -> set[str]: ...

    def collect_disk(self) -> DiskMetrics:
        # e.g. {"memory", "cpu", "gpu", "pressure", "disk"}
        # Callers check this before trusting Optional fields
        """
        Cross-platform disk usage for the system root volume.
        psutil.disk_usage() is identical on macOS, Linux, and Windows —
        no need to override in platform subclasses.
        A full root disk crashes model downloads and can corrupt Ollama's
        model index, making this a safety metric rather than informational.
        """
        path = "C:\\" if sys.platform == "win32" else "/"
        usage = psutil.disk_usage(path)
        return DiskMetrics(
            path=path,
            total_mb=usage.total // (1024 * 1024),
            used_mb=usage.used // (1024 * 1024),
            free_mb=usage.free // (1024 * 1024),
            used_pct=usage.percent,
        )


@dataclass
class ResourceThresholds:
    ram_pause_pct: float = 85.0
    # must be < ram_pause_pct — gap prevents pause→resume→pause cycling;
    # reloading the model re-triggers the threshold
    ram_resume_pct: float = 60.0
    cpu_pause_pct: float = 90.0
    cpu_sustained_seconds: int = 30  # must exceed threshold for this long before acting
    gpu_vram_pause_pct: float = 85.0
    pause_timeout_seconds: int = 120  # grace period before resume after resource clears
    check_interval_seconds: int = 10
    # When False: RAM-triggered pauses require manual /resume — prevents oscillation
    # on machines where the model itself is the dominant RAM consumer.
    auto_resume_on_ram_pressure: bool = True


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

        - RAM: pause when used_pct >= ram_pause_pct.
        - CPU: caller enforces the sustained window; this returns True whenever
               cpu exceeds the threshold so the caller can count ticks.
        - GPU VRAM: pause immediately when vram_used_pct >= gpu_vram_pause_pct.
        - Resume: returns False only when ALL metrics are below their resume thresholds.
          The resume check is separated into evaluate_resume() so callers can
          apply the grace-period window independently.

        NOTE — memory_pressure level is intentionally NOT used as an independent
        pause trigger. On Apple Silicon, loading a large model into unified memory
        routinely produces transient CRITICAL pressure readings even when RAM% is
        within the user's configured threshold. Using CRITICAL as an override
        defeats the purpose of ram_pause_pct configuration. Pressure level is
        still reported in /metrics for informational purposes.
        """
        t = self._t
        mem = metrics.memory
        cpu = metrics.cpu
        gpu = metrics.gpu

        if mem.used_pct >= t.ram_pause_pct:
            return True, f"RAM {mem.used_pct:.1f}% >= {t.ram_pause_pct}% threshold"

        # CPU (caller accumulates ticks; we just report the instantaneous check)
        if cpu.used_pct >= t.cpu_pause_pct:
            return True, f"CPU {cpu.used_pct:.1f}% >= {t.cpu_pause_pct}% threshold"

        # GPU VRAM
        if gpu.available and gpu.vram_used_pct is not None:
            if gpu.vram_used_pct >= t.gpu_vram_pause_pct:
                return (
                    True,
                    f"GPU VRAM {gpu.vram_used_pct:.1f}% >= {t.gpu_vram_pause_pct}% threshold",
                )

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

        if mem.used_pct >= t.ram_resume_pct:
            return False, f"RAM {mem.used_pct:.1f}% >= resume threshold {t.ram_resume_pct}%"

        if cpu.used_pct >= t.cpu_pause_pct:
            return False, f"CPU {cpu.used_pct:.1f}% still elevated"

        if gpu.available and gpu.vram_used_pct is not None:
            if gpu.vram_used_pct >= t.gpu_vram_pause_pct:
                return False, f"GPU VRAM {gpu.vram_used_pct:.1f}% still elevated"

        return True, "all resource metrics below resume thresholds"
