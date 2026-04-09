"""
Tests for resource collector logic — pure logic helpers + collector shape.

Covers:
  - _pressure_from_pct() boundary values (shared by Windows + Linux)
  - GPU helper error paths (_try_nvidia, _try_wmi, _try_amd_sysfs) when
    optional dependencies are absent or raise
  - WindowsResourceCollector.collect() returns a valid SystemMetrics shape
  - LinuxResourceCollector.collect() returns a valid SystemMetrics shape

All platform-specific I/O (psutil, pynvml, wmi, sysfs) is mocked so these
tests run on any OS in CI.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from llm_valet.resources.base import (
    CPUMetrics,
    DiskMetrics,
    GPUMetrics,
    MemoryMetrics,
    PressureLevel,
    SystemMetrics,
)


# ── _pressure_from_pct — shared by Windows and Linux ─────────────────────────

class TestPressureFromPct:
    """
    _pressure_from_pct is identical in windows.py and linux.py.
    We import from windows.py; the same thresholds apply to both.
    """

    @pytest.fixture(autouse=True)
    def import_fn(self):
        from llm_valet.resources.windows import _pressure_from_pct
        self.fn = _pressure_from_pct

    def test_normal_at_zero(self) -> None:
        assert self.fn(0.0) == PressureLevel.NORMAL

    def test_normal_below_75(self) -> None:
        assert self.fn(74.9) == PressureLevel.NORMAL

    def test_warn_at_75(self) -> None:
        assert self.fn(75.0) == PressureLevel.WARN

    def test_warn_at_89(self) -> None:
        assert self.fn(89.9) == PressureLevel.WARN

    def test_critical_at_90(self) -> None:
        assert self.fn(90.0) == PressureLevel.CRITICAL

    def test_critical_at_100(self) -> None:
        assert self.fn(100.0) == PressureLevel.CRITICAL

    def test_linux_pressure_matches_windows(self) -> None:
        """Linux and Windows use the same thresholds — verify they're in sync."""
        from llm_valet.resources.linux import _pressure_from_pct as linux_fn
        from llm_valet.resources.windows import _pressure_from_pct as win_fn
        for pct in (0.0, 50.0, 74.9, 75.0, 89.9, 90.0, 100.0):
            assert linux_fn(pct) == win_fn(pct), f"mismatch at {pct}%"


# ── _try_nvidia — import / exception paths ────────────────────────────────────

class TestTryNvidia:
    def _call(self, platform: str) -> GPUMetrics | None:
        if platform == "windows":
            from llm_valet.resources.windows import _try_nvidia
        else:
            from llm_valet.resources.linux import _try_nvidia
        return _try_nvidia()

    @pytest.mark.parametrize("platform", ["windows", "linux"])
    def test_returns_none_when_pynvml_not_installed(self, platform: str) -> None:
        with patch.dict(sys.modules, {"pynvml": None}):
            result = self._call(platform)
        assert result is None

    @pytest.mark.parametrize("platform", ["windows", "linux"])
    def test_returns_none_when_nvmlinit_raises(self, platform: str) -> None:
        mock_nvml = MagicMock()
        mock_nvml.nvmlInit.side_effect = Exception("no GPU")
        with patch.dict(sys.modules, {"pynvml": mock_nvml}):
            result = self._call(platform)
        assert result is None

    @pytest.mark.parametrize("platform", ["windows", "linux"])
    def test_returns_gpu_metrics_when_pynvml_available(self, platform: str) -> None:
        mock_nvml = MagicMock()
        mock_nvml.nvmlInit.return_value = None
        mem_info = MagicMock()
        mem_info.total = 8 * 1024 * 1024 * 1024  # 8 GB
        mem_info.used  = 4 * 1024 * 1024 * 1024  # 4 GB
        util_info = MagicMock()
        util_info.gpu = 30
        mock_nvml.nvmlDeviceGetHandleByIndex.return_value = MagicMock()
        mock_nvml.nvmlDeviceGetMemoryInfo.return_value  = mem_info
        mock_nvml.nvmlDeviceGetUtilizationRates.return_value = util_info
        with patch.dict(sys.modules, {"pynvml": mock_nvml}):
            result = self._call(platform)
        assert result is not None
        assert result.available is True
        assert result.vram_total_mb == 8192
        assert result.vram_used_mb == 4096
        assert result.vram_used_pct == 50.0
        assert result.compute_pct == 30.0


# ── _try_wmi — Windows-only ───────────────────────────────────────────────────

class TestTryWmi:
    def _call(self) -> GPUMetrics | None:
        from llm_valet.resources.windows import _try_wmi
        return _try_wmi()

    def test_returns_none_when_wmi_not_installed(self) -> None:
        with patch.dict(sys.modules, {"wmi": None}):
            result = self._call()
        assert result is None

    def test_returns_none_when_wmi_raises(self) -> None:
        mock_wmi_mod = MagicMock()
        mock_wmi_mod.WMI.side_effect = Exception("WMI error")
        with patch.dict(sys.modules, {"wmi": mock_wmi_mod}):
            result = self._call()
        assert result is None

    def test_returns_none_when_no_controllers(self) -> None:
        mock_wmi_mod = MagicMock()
        mock_wmi_mod.WMI.return_value.Win32_VideoController.return_value = []
        with patch.dict(sys.modules, {"wmi": mock_wmi_mod}):
            result = self._call()
        assert result is None

    def test_returns_gpu_metrics_with_adapter_ram(self) -> None:
        ctrl = MagicMock()
        ctrl.AdapterRAM = 4 * 1024 * 1024 * 1024  # 4 GB
        mock_wmi_mod = MagicMock()
        mock_wmi_mod.WMI.return_value.Win32_VideoController.return_value = [ctrl]
        with patch.dict(sys.modules, {"wmi": mock_wmi_mod}):
            result = self._call()
        assert result is not None
        assert result.available is True
        assert result.vram_total_mb == 4096
        assert result.vram_used_mb is None    # WMI only exposes total
        assert result.vram_used_pct is None

    def test_returns_partial_when_adapter_ram_zero(self) -> None:
        """WMI returns 0 for VRAM > 4 GB (32-bit overflow) — must not crash."""
        ctrl = MagicMock()
        ctrl.AdapterRAM = 0
        mock_wmi_mod = MagicMock()
        mock_wmi_mod.WMI.return_value.Win32_VideoController.return_value = [ctrl]
        with patch.dict(sys.modules, {"wmi": mock_wmi_mod}):
            result = self._call()
        assert result is not None
        assert result.available is True
        assert result.vram_total_mb is None  # reported unknown


# ── _try_amd_sysfs — Linux-only ───────────────────────────────────────────────

class TestTryAmdSysfs:
    def _call(self) -> GPUMetrics | None:
        from llm_valet.resources.linux import _try_amd_sysfs
        return _try_amd_sysfs()

    def _mock_path(self, is_file: bool = True, total: int = 0, used: int = 0,
                   read_exc: Exception | None = None) -> MagicMock:
        """Build a mock Path class whose instances report the given sysfs values."""
        def make_instance(path_str: str) -> MagicMock:
            inst = MagicMock()
            inst.__str__ = lambda s: path_str
            inst.is_file.return_value = is_file
            if read_exc:
                inst.read_text.side_effect = read_exc
            elif "total" in path_str:
                inst.read_text.return_value = str(total)
            else:
                inst.read_text.return_value = str(used)
            return inst

        mock_cls = MagicMock(side_effect=make_instance)
        return mock_cls

    def test_returns_none_when_sysfs_absent(self) -> None:
        with patch("llm_valet.resources.linux.Path", self._mock_path(is_file=False)):
            result = self._call()
        assert result is None

    def test_returns_gpu_metrics_from_sysfs(self) -> None:
        total_bytes = 8 * 1024 * 1024 * 1024  # 8 GB
        used_bytes  = 2 * 1024 * 1024 * 1024  # 2 GB
        with patch("llm_valet.resources.linux.Path",
                   self._mock_path(total=total_bytes, used=used_bytes)):
            result = self._call()

        assert result is not None
        assert result.available is True
        assert result.vram_total_mb == 8192
        assert result.vram_used_mb  == 2048
        assert result.vram_used_pct == 25.0

    def test_returns_none_on_read_error(self) -> None:
        with patch("llm_valet.resources.linux.Path",
                   self._mock_path(read_exc=OSError("permission denied"))):
            result = self._call()
        assert result is None


# ── WindowsResourceCollector.collect() — shape check ─────────────────────────

class TestWindowsCollectorShape:
    """Verify collect() returns a valid SystemMetrics regardless of GPU availability."""

    def _make_psutil_vm(self) -> MagicMock:
        vm = MagicMock()
        vm.total   = 16 * 1024 * 1024 * 1024
        vm.used    = 8  * 1024 * 1024 * 1024
        vm.percent = 50.0
        return vm

    def test_collect_returns_system_metrics(self) -> None:
        from llm_valet.resources.windows import WindowsResourceCollector
        vm = self._make_psutil_vm()
        with (
            patch("llm_valet.resources.windows.psutil.virtual_memory", return_value=vm),
            patch("llm_valet.resources.windows.psutil.cpu_percent", return_value=10.0),
            patch("llm_valet.resources.windows.psutil.cpu_count", return_value=8),
            patch("llm_valet.resources.windows.psutil.disk_usage") as mock_disk,
            patch.dict(sys.modules, {"pynvml": None, "wmi": None}),
        ):
            mock_disk.return_value = MagicMock(
                total=500 * 1024**3, used=100 * 1024**3,
                free=400 * 1024**3, percent=20.0,
            )
            result = WindowsResourceCollector().collect()

        assert isinstance(result, SystemMetrics)
        assert isinstance(result.memory, MemoryMetrics)
        assert isinstance(result.cpu, CPUMetrics)
        assert isinstance(result.gpu, GPUMetrics)
        assert isinstance(result.disk, DiskMetrics)

    def test_memory_fields_populated(self) -> None:
        from llm_valet.resources.windows import WindowsResourceCollector
        vm = self._make_psutil_vm()
        with (
            patch("llm_valet.resources.windows.psutil.virtual_memory", return_value=vm),
            patch("llm_valet.resources.windows.psutil.cpu_percent", return_value=10.0),
            patch("llm_valet.resources.windows.psutil.cpu_count", return_value=8),
            patch("llm_valet.resources.windows.psutil.disk_usage") as mock_disk,
            patch.dict(sys.modules, {"pynvml": None, "wmi": None}),
        ):
            mock_disk.return_value = MagicMock(
                total=500 * 1024**3, used=100 * 1024**3,
                free=400 * 1024**3, percent=20.0,
            )
            result = WindowsResourceCollector().collect()

        assert result.memory.total_mb == 16384
        assert result.memory.used_mb  == 8192
        assert result.memory.used_pct == 50.0
        assert result.memory.pressure == PressureLevel.NORMAL

    def test_gpu_unavailable_when_no_drivers(self) -> None:
        from llm_valet.resources.windows import WindowsResourceCollector
        vm = self._make_psutil_vm()
        with (
            patch("llm_valet.resources.windows.psutil.virtual_memory", return_value=vm),
            patch("llm_valet.resources.windows.psutil.cpu_percent", return_value=10.0),
            patch("llm_valet.resources.windows.psutil.cpu_count", return_value=8),
            patch("llm_valet.resources.windows.psutil.disk_usage") as mock_disk,
            patch.dict(sys.modules, {"pynvml": None, "wmi": None}),
        ):
            mock_disk.return_value = MagicMock(
                total=500 * 1024**3, used=100 * 1024**3,
                free=400 * 1024**3, percent=20.0,
            )
            result = WindowsResourceCollector().collect()

        assert result.gpu.available is False
        assert result.gpu.vram_total_mb is None

    def test_supported_metrics_includes_expected_keys(self) -> None:
        from llm_valet.resources.windows import WindowsResourceCollector
        metrics = WindowsResourceCollector().supported_metrics()
        assert "memory" in metrics
        assert "cpu" in metrics
        assert "gpu" in metrics
        assert "disk" in metrics
