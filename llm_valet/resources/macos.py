import logging
import plistlib
import subprocess
from typing import Any

import psutil

from llm_valet.resources.base import (
    CPUMetrics,
    GPUMetrics,
    MemoryMetrics,
    PressureLevel,
    ResourceCollector,
    SystemMetrics,
)

logger = logging.getLogger(__name__)


class MacOSResourceCollector(ResourceCollector):

    def collect(self) -> SystemMetrics:
        return SystemMetrics(
            memory=self._collect_memory(),
            cpu=self._collect_cpu(),
            gpu=self._collect_gpu(),
        )

    def supported_metrics(self) -> set[str]:
        return {"memory", "cpu", "gpu", "pressure"}

    # ── Memory ────────────────────────────────────────────────────────────────

    def _collect_memory(self) -> MemoryMetrics:
        vm = psutil.virtual_memory()
        return MemoryMetrics(
            total_mb=vm.total // (1024 * 1024),
            used_mb=vm.used // (1024 * 1024),
            used_pct=vm.percent,
            pressure=self._read_memory_pressure(),
        )

    def _read_memory_pressure(self) -> PressureLevel:
        """
        Reads macOS memory pressure via the `memory_pressure` CLI.
        On Apple Silicon, this reflects unified memory pressure — more meaningful
        than raw RAM % because GPU and CPU share the same pool.
        Falls back to psutil thresholds if the CLI is unavailable.
        """
        try:
            result = subprocess.run(
                ["memory_pressure"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = result.stdout.lower()
            if "critical" in output:
                return PressureLevel.CRITICAL
            if "warn" in output:
                return PressureLevel.WARN
            return PressureLevel.NORMAL
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            logger.warning("memory_pressure CLI unavailable — falling back to psutil thresholds")
            pct = psutil.virtual_memory().percent
            if pct >= 90.0:
                return PressureLevel.CRITICAL
            if pct >= 75.0:
                return PressureLevel.WARN
            return PressureLevel.NORMAL

    # ── CPU ───────────────────────────────────────────────────────────────────

    def _collect_cpu(self) -> CPUMetrics:
        return CPUMetrics(
            used_pct=psutil.cpu_percent(interval=1),
            core_count=psutil.cpu_count(logical=True) or 1,
        )

    # ── GPU ───────────────────────────────────────────────────────────────────

    def _collect_gpu(self) -> GPUMetrics:
        """
        Queries IOAccelerator via `ioreg -r -c IOAccelerator -a` for GPU memory stats.
        Apple Silicon has unified memory — discrete VRAM keys are absent; best-effort
        alloc stats are read from PerformanceStatistics instead.
        Returns available=True with None metrics when the GPU is present but stats are
        not parseable; available=False only when the subprocess itself fails.
        """
        try:
            result = subprocess.run(
                ["ioreg", "-r", "-c", "IOAccelerator", "-a"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return _gpu_present_no_stats()

            data = plistlib.loads(result.stdout)
            if not isinstance(data, list) or not data:
                return _gpu_present_no_stats()

            accel = data[0]
            perf: dict[str, Any] = accel.get("PerformanceStatistics", {})

            # Discrete GPU (Intel/AMD eGPU): exposes VRAM,totalMB
            vram_total_bytes = (accel.get("VRAM,totalMB") or 0) * 1024 * 1024

            # Apple Silicon: best-effort alloc stats from PerformanceStatistics
            used_bytes: int = (
                perf.get("Alloc system memory")
                or perf.get("IOSurface Local Alloc")
                or 0
            )

            if vram_total_bytes and used_bytes:
                return GPUMetrics(
                    available=True,
                    vram_total_mb=vram_total_bytes // (1024 * 1024),
                    vram_used_mb=used_bytes // (1024 * 1024),
                    vram_used_pct=round((used_bytes / vram_total_bytes) * 100, 1),
                    compute_pct=None,
                )

            return _gpu_present_no_stats()

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError) as exc:
            logger.warning("GPU metrics unavailable", extra={"reason": str(exc)})
            return GPUMetrics(
                available=False,
                vram_total_mb=None,
                vram_used_mb=None,
                vram_used_pct=None,
                compute_pct=None,
            )


def _gpu_present_no_stats() -> GPUMetrics:
    return GPUMetrics(
        available=True,
        vram_total_mb=None,
        vram_used_mb=None,
        vram_used_pct=None,
        compute_pct=None,
    )
