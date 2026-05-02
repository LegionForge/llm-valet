"""
Unit tests for OllamaProvider.force_pause() and runner-kill helpers.

All psutil calls are mocked — no real process enumeration or signal sending.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import psutil

from llm_valet.providers.base import ProviderStatus
from llm_valet.providers.ollama import (
    OllamaProvider,
    _is_ollama_runner,
    _kill_ollama_runners,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_proc(name: str = "", exe: str = "", cmdline: list[str] | None = None) -> MagicMock:
    proc = MagicMock(spec=psutil.Process)
    proc.pid = 1234
    proc.name.return_value = name
    proc.exe.return_value = exe
    proc.cmdline.return_value = cmdline or []
    return proc


def _make_status(model_name: str | None = "qwen3.5:0.8b", ctx: int | None = 4096) -> ProviderStatus:
    return ProviderStatus(
        running=True,
        model_loaded=model_name is not None,
        model_name=model_name,
        memory_used_mb=2000 if model_name else None,
        loaded_context_length=ctx,
    )


# ── _is_ollama_runner() ───────────────────────────────────────────────────────


class TestIsOllamaRunner:
    def test_ollama_llama_runner_in_name(self) -> None:
        proc = _make_proc(name="ollama_llama_runner", exe="/usr/bin/ollama_llama_runner")
        assert _is_ollama_runner(proc) is True

    def test_ollama_llama_runner_in_exe(self) -> None:
        proc = _make_proc(name="ollama", exe="/usr/bin/ollama_llama_runner")
        assert _is_ollama_runner(proc) is True

    def test_ollama_runner_subcommand(self) -> None:
        proc = _make_proc(
            name="ollama",
            exe="/usr/local/bin/ollama",
            cmdline=["ollama", "runner", "--model", "qwen3.5:0.8b"],
        )
        assert _is_ollama_runner(proc) is True

    def test_ollama_serve_not_a_runner(self) -> None:
        """The Ollama server process must never match as a runner."""
        proc = _make_proc(
            name="ollama",
            exe="/usr/local/bin/ollama",
            cmdline=["ollama", "serve"],
        )
        assert _is_ollama_runner(proc) is False

    def test_unrelated_process_not_a_runner(self) -> None:
        proc = _make_proc(name="python3", exe="/usr/bin/python3", cmdline=["python3", "app.py"])
        assert _is_ollama_runner(proc) is False

    def test_no_such_process_returns_false(self) -> None:
        proc = _make_proc(name="ollama_llama_runner")
        proc.name.side_effect = psutil.NoSuchProcess(1234)
        assert _is_ollama_runner(proc) is False

    def test_access_denied_returns_false(self) -> None:
        proc = _make_proc(name="ollama_llama_runner")
        proc.exe.side_effect = psutil.AccessDenied(1234)
        assert _is_ollama_runner(proc) is False


# ── _kill_ollama_runners() ────────────────────────────────────────────────────


class TestKillOllamaRunners:
    def test_kills_matching_process_returns_count(self) -> None:
        runner = _make_proc(name="ollama_llama_runner", exe="/usr/bin/ollama_llama_runner")
        with patch("llm_valet.providers.ollama.psutil.process_iter", return_value=[runner]):
            count = _kill_ollama_runners()
        assert count == 1
        runner.kill.assert_called_once()

    def test_ignores_non_runner_processes(self) -> None:
        other = _make_proc(name="python3", exe="/usr/bin/python3")
        with patch("llm_valet.providers.ollama.psutil.process_iter", return_value=[other]):
            count = _kill_ollama_runners()
        assert count == 0
        other.kill.assert_not_called()

    def test_returns_zero_when_no_runners(self) -> None:
        with patch("llm_valet.providers.ollama.psutil.process_iter", return_value=[]):
            count = _kill_ollama_runners()
        assert count == 0

    def test_skips_process_that_vanishes_during_kill(self) -> None:
        runner = _make_proc(name="ollama_llama_runner", exe="/usr/bin/ollama_llama_runner")
        runner.kill.side_effect = psutil.NoSuchProcess(1234)
        with patch("llm_valet.providers.ollama.psutil.process_iter", return_value=[runner]):
            count = _kill_ollama_runners()
        # NoSuchProcess is swallowed — process is already gone, which is fine
        assert count == 0

    def test_kills_multiple_runners(self) -> None:
        r1 = _make_proc(name="ollama_llama_runner", exe="/bin/ollama_llama_runner")
        r2 = _make_proc(name="ollama_llama_runner", exe="/bin/ollama_llama_runner")
        with patch("llm_valet.providers.ollama.psutil.process_iter", return_value=[r1, r2]):
            count = _kill_ollama_runners()
        assert count == 2
        r1.kill.assert_called_once()
        r2.kill.assert_called_once()


# ── OllamaProvider.force_pause() ─────────────────────────────────────────────


class TestForcePause:
    async def test_kills_runners_and_calls_pause(self) -> None:
        # force_pause always calls pause() after killing runners — killing alone is not
        # sufficient because Ollama may restart the runner; keep_alive=0 prevents that.
        provider = OllamaProvider()
        provider.status = AsyncMock(return_value=_make_status())  # type: ignore[method-assign]
        provider.pause = AsyncMock(return_value=True)  # type: ignore[method-assign]
        with patch("llm_valet.providers.ollama._kill_ollama_runners", return_value=1):
            result = await provider.force_pause()
        provider.pause.assert_called_once()
        assert result is True

    async def test_captures_model_name_before_kill(self) -> None:
        provider = OllamaProvider()
        provider.status = AsyncMock(return_value=_make_status(model_name="mistral:7b", ctx=16384))  # type: ignore[method-assign]
        provider.pause = AsyncMock(return_value=True)  # type: ignore[method-assign]
        with patch("llm_valet.providers.ollama._kill_ollama_runners", return_value=1):
            await provider.force_pause()
        assert provider._last_loaded_model == "mistral:7b"
        assert provider._last_loaded_ctx == 16384

    async def test_falls_back_to_pause_when_no_runners_found(self) -> None:
        provider = OllamaProvider()
        provider.status = AsyncMock(return_value=_make_status())  # type: ignore[method-assign]
        provider.pause = AsyncMock(return_value=True)  # type: ignore[method-assign]
        with patch("llm_valet.providers.ollama._kill_ollama_runners", return_value=0):
            result = await provider.force_pause()
        provider.pause.assert_called_once()
        assert result is True

    async def test_handles_no_model_loaded(self) -> None:
        """force_pause with no model loaded — should not crash; falls back to pause()."""
        provider = OllamaProvider()
        provider.status = AsyncMock(return_value=_make_status(model_name=None, ctx=None))  # type: ignore[method-assign]
        provider.pause = AsyncMock(return_value=True)  # type: ignore[method-assign]
        with patch("llm_valet.providers.ollama._kill_ollama_runners", return_value=0):
            result = await provider.force_pause()
        assert result is True
        # _last_loaded_model should not be overwritten with None
        assert provider._last_loaded_model is None

    async def test_ctx_preserved_for_resume_after_force(self) -> None:
        """After force_pause, _last_loaded_ctx must be set so resume() can restore it."""
        provider = OllamaProvider()
        provider.status = AsyncMock(  # type: ignore[method-assign]
            return_value=_make_status(model_name="qwen3.5:0.8b", ctx=65536)
        )
        provider.pause = AsyncMock(return_value=True)  # type: ignore[method-assign]
        with patch("llm_valet.providers.ollama._kill_ollama_runners", return_value=1):
            await provider.force_pause()
        assert provider._last_loaded_ctx == 65536
