"""
Windows ResourceCollector.

Metrics:
  RAM / CPU   — psutil
  Memory pressure — derived from psutil thresholds
  GPU VRAM    — pynvml (NVIDIA) if available; WMI fallback
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


class WindowsResourceCollector(ResourceCollector):

    def collect(self) -> SystemMetrics:
        return SystemMetrics(
            memory=self._collect_memory(),
            cpu=self._collect_cpu(),
            gpu=self._collect_gpu(),
        )

    def supported_metrics(self) -> set[str]:
        return {"memory", "cpu", "gpu"}

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
        gpu = _try_nvidia()
        if gpu is not None:
            return gpu
        gpu = _try_wmi()
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
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        mem  = pynvml.nvmlDeviceGetMemoryInfo(handle)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        total_mb = mem.total // (1024 * 1024)
        used_mb  = mem.used  // (1024 * 1024)
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


def _try_wmi() -> GPUMetrics | None:
    """
    WMI fallback for GPU VRAM — works for NVIDIA, AMD, and Intel Arc on Windows.
    Uses Win32_VideoController.AdapterRAM (total only; used is not exposed via WMI).
    """
    try:
        import wmi  # optional — install with: pip install wmi
        w = wmi.WMI()
        controllers = w.Win32_VideoController()
        if not controllers:
            return None
        ctrl = controllers[0]
        adapter_ram = getattr(ctrl, "AdapterRAM", None)
        if not adapter_ram:
            # WMI returns 0 or None when VRAM > 4 GB (32-bit field overflow)
            logger.debug("WMI AdapterRAM is 0 or unavailable — VRAM > 4 GB or unsupported")
            return GPUMetrics(
                available=True,
                vram_total_mb=None,
                vram_used_mb=None,
                vram_used_pct=None,
                compute_pct=None,
            )
        total_mb = int(adapter_ram) // (1024 * 1024)
        return GPUMetrics(
            available=True,
            vram_total_mb=total_mb,
            vram_used_mb=None,     # WMI does not expose used VRAM
            vram_used_pct=None,
            compute_pct=None,
        )
    except ImportError:
        logger.debug("wmi package not installed — GPU metrics unavailable on Windows")
    except Exception as exc:
        logger.debug("WMI query failed", extra={"error": str(exc)})
    return None


def _pressure_from_pct(pct: float) -> PressureLevel:
    if pct >= 90.0:
        return PressureLevel.CRITICAL
    if pct >= 75.0:
        return PressureLevel.WARN
    return PressureLevel.NORMAL
