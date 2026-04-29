"""
Linux ResourceCollector.

Metrics:
  RAM / CPU   — psutil
  Memory pressure — derived from psutil thresholds (no OS pressure API on Linux)
  GPU VRAM    — pynvml (NVIDIA) if available; /sys/class/drm fallback for AMD
"""

import logging

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


class LinuxResourceCollector(ResourceCollector):
    def collect(self) -> SystemMetrics:
        return SystemMetrics(
            memory=self._collect_memory(),
            cpu=self._collect_cpu(),
            gpu=self._collect_gpu(),
            disk=self.collect_disk(),
        )

    def supported_metrics(self) -> set[str]:
        return {"memory", "cpu", "gpu", "disk"}

    # ── Memory ────────────────────────────────────────────────────────────────

    def _collect_memory(self) -> MemoryMetrics:
        vm = psutil.virtual_memory()
        return MemoryMetrics(
            total_mb=vm.total // (1024 * 1024),
            used_mb=vm.used // (1024 * 1024),
            used_pct=vm.percent,
            pressure=_pressure_from_pct(vm.percent),
        )

    # ── CPU ───────────────────────────────────────────────────────────────────

    def _collect_cpu(self) -> CPUMetrics:
        return CPUMetrics(
            used_pct=psutil.cpu_percent(interval=1),
            core_count=psutil.cpu_count(logical=True) or 1,
        )

    # ── GPU ───────────────────────────────────────────────────────────────────

    def _collect_gpu(self) -> GPUMetrics:
        # Try NVIDIA first via pynvml
        gpu = _try_nvidia()
        if gpu is not None:
            return gpu
        # Try AMD via sysfs
        gpu = _try_amd_sysfs()
        if gpu is not None:
            return gpu
        return GPUMetrics(
            available=False,
            vram_total_mb=None,
            vram_used_mb=None,
            vram_used_pct=None,
            compute_pct=None,
        )


# ── GPU helpers ───────────────────────────────────────────────────────────────


def _try_nvidia() -> GPUMetrics | None:
    try:
        import pynvml  # optional dependency

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        total_mb = mem.total // (1024 * 1024)
        used_mb = mem.used // (1024 * 1024)
        return GPUMetrics(
            available=True,
            vram_total_mb=total_mb,
            vram_used_mb=used_mb,
            vram_used_pct=round((mem.used / mem.total) * 100, 1) if mem.total else None,
            compute_pct=float(util.gpu),
        )
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("pynvml unavailable", extra={"error": str(exc)})
    return None


def _try_amd_sysfs() -> GPUMetrics | None:
    """
    AMD GPU VRAM via /sys/class/drm/card0/device/mem_info_vram_{total,used}.
    These files are present on amdgpu driver (not radeon legacy).
    """
    from pathlib import Path

    total_path = Path("/sys/class/drm/card0/device/mem_info_vram_total")
    used_path = Path("/sys/class/drm/card0/device/mem_info_vram_used")

    if not total_path.is_file() or not used_path.is_file():
        return None

    try:
        total_bytes = int(total_path.read_text().strip())
        used_bytes = int(used_path.read_text().strip())
        total_mb = total_bytes // (1024 * 1024)
        used_mb = used_bytes // (1024 * 1024)
        return GPUMetrics(
            available=True,
            vram_total_mb=total_mb,
            vram_used_mb=used_mb,
            vram_used_pct=round((used_bytes / total_bytes) * 100, 1) if total_bytes else None,
            compute_pct=None,
        )
    except (ValueError, OSError) as exc:
        logger.debug("AMD sysfs read error", extra={"error": str(exc)})
    return None


def _pressure_from_pct(pct: float) -> PressureLevel:
    if pct >= 90.0:
        return PressureLevel.CRITICAL
    if pct >= 75.0:
        return PressureLevel.WARN
    return PressureLevel.NORMAL
