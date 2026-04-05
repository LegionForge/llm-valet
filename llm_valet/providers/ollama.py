import asyncio
import logging
import re
from typing import Any

import httpx
import psutil

from llm_valet.providers.base import LLMProvider, ProviderStatus

logger = logging.getLogger(__name__)

# Model name validation — must match before any subprocess/API use
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9:._-]+$")

# How long to wait for graceful shutdown before escalating
_SIGTERM_TIMEOUT_S = 30
_POLL_INTERVAL_S = 1


class OllamaProvider(LLMProvider):
    """
    Ollama provider implementation.

    Pause/resume operate at the model level via keep_alive:
      pause  — POST /api/generate {model, keep_alive: 0}  → evicts model from memory
      resume — POST /api/generate {model, keep_alive: -1} → pre-warms model into memory

    Stop/start operate at the process level via psutil (no shell, no subprocess).
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model_name: str | None = None,
        request_timeout: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._timeout = request_timeout
        # Cached at pause time — /api/ps is empty after eviction, so resume
        # needs to remember which model to reload.
        self._last_loaded_model: str | None = None

    # ── LLMProvider interface ─────────────────────────────────────────────────

    async def pause(self) -> bool:
        """Evict the loaded model from memory via keep_alive=0. Service stays running."""
        model = await self._resolve_model()
        if model is None:
            logger.info("pause skipped — no model currently loaded")
            return True

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    json={"model": model, "keep_alive": 0},
                )
                resp.raise_for_status()
                data = resp.json()
                success = data.get("done_reason") == "unload"
                if success:
                    self._last_loaded_model = model
                    logger.info("model unloaded", extra={"model": model})
                else:
                    logger.warning(
                        "unexpected pause response",
                        extra={"model": model, "done_reason": data.get("done_reason")},
                    )
                return bool(success)
        except httpx.HTTPError as exc:
            logger.error("pause request failed", extra={"error": str(exc)})
            return False

    async def resume(self) -> bool:
        """Pre-warm the model into memory via keep_alive=-1."""
        model = await self._resolve_model()
        if model is None:
            logger.warning("resume skipped — no model name available")
            return False

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    json={"model": model, "keep_alive": -1},
                )
                resp.raise_for_status()
                logger.info("model pre-warmed", extra={"model": model})
                return True
        except httpx.HTTPError as exc:
            logger.error("resume request failed", extra={"error": str(exc)})
            return False

    async def start(self) -> bool:
        """
        Start the Ollama service via platform service manager.
        This method is intentionally thin — the heavy lifting belongs in
        svcmgr/macos.py (launchctl) which is wired up at a higher layer.
        Here we just verify the service comes up within the timeout.
        """
        if await self.health_check():
            logger.info("start called but Ollama is already running")
            return True
        logger.info("waiting for Ollama to start")
        for _ in range(30):
            await asyncio.sleep(2)
            if await self.health_check():
                logger.info("Ollama is up")
                return True
        logger.error("Ollama did not come up within timeout")
        return False

    async def stop(self) -> bool:
        """
        Gracefully stop the Ollama service.

        Sequence:
          1. pause() — unload model cleanly
          2. SIGTERM to ollama serve process
          3. Poll health_check() until False (30s timeout)
          4. SIGKILL if still alive
        """
        await self.pause()

        proc = _find_ollama_process()
        if proc is None:
            logger.info("stop called but no Ollama process found")
            return True

        logger.info("sending SIGTERM to Ollama", extra={"pid": proc.pid})
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            return True
        except psutil.AccessDenied:
            logger.error("SIGTERM denied — check process ownership")
            return False

        # Poll until dead or timeout
        for _ in range(_SIGTERM_TIMEOUT_S // _POLL_INTERVAL_S):
            await asyncio.sleep(_POLL_INTERVAL_S)
            if not await self.health_check():
                logger.info("Ollama stopped gracefully")
                return True

        # Escalate to SIGKILL
        logger.warning(
            "Ollama did not stop after SIGTERM — escalating to SIGKILL",
            extra={"pid": proc.pid},
        )
        try:
            # Re-validate before SIGKILL — PID may have been reused
            if proc.is_running() and _is_ollama_process(proc):
                proc.kill()
                await asyncio.sleep(2)
                logger.info("Ollama killed")
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        return not await self.health_check()

    async def status(self) -> ProviderStatus:
        running = await self.health_check()
        if not running:
            return ProviderStatus(
                running=False,
                model_loaded=False,
                model_name=None,
                memory_used_mb=None,
            )

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self._base_url}/api/ps")
                resp.raise_for_status()
                models: list[dict[str, Any]] = resp.json().get("models", [])

            if not models:
                return ProviderStatus(
                    running=True,
                    model_loaded=False,
                    model_name=None,
                    memory_used_mb=None,
                )

            loaded = models[0]
            size_mb = (int(loaded.get("size") or 0)) // (1024 * 1024) or None
            return ProviderStatus(
                running=True,
                model_loaded=True,
                model_name=str(loaded.get("name", "")) or None,
                memory_used_mb=size_mb,
            )
        except httpx.HTTPError as exc:
            logger.error("status request failed", extra={"error": str(exc)})
            return ProviderStatus(
                running=True,
                model_loaded=False,
                model_name=None,
                memory_used_mb=None,
            )

    async def health_check(self) -> bool:
        """GET /api/tags — fast liveness probe."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _resolve_model(self) -> str | None:
        """
        Return the model name to act on. Resolution order:
          1. Configured model_name (config.yaml / constructor arg)
          2. First model currently loaded in /api/ps
          3. Last model we successfully paused (_last_loaded_model cache)
             — /api/ps is empty after eviction, so resume needs this fallback.
        Returns None if no model name can be determined by any method.
        """
        if self._model_name:
            if not _MODEL_NAME_RE.match(self._model_name):
                logger.error(
                    "configured model name failed validation",
                    extra={"model": self._model_name},
                )
                return None
            return self._model_name

        # Check what is currently loaded
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self._base_url}/api/ps")
                resp.raise_for_status()
                models = resp.json().get("models", [])
                if models:
                    return str(models[0].get("name", "")) or None
        except httpx.HTTPError:
            pass

        # Fall back to the last model we paused — lets resume work after eviction
        if self._last_loaded_model:
            logger.info(
                "model not in /api/ps — using last known model",
                extra={"model": self._last_loaded_model},
            )
            return self._last_loaded_model

        return None


# ── Module-level process helpers (no shell, no injection surface) ─────────────

def _find_ollama_process() -> psutil.Process | None:
    """
    Scan running processes for the Ollama server process.
    Validates name and exe path before returning — guards against PID reuse.
    """
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if _is_ollama_process(proc):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _is_ollama_process(proc: psutil.Process) -> bool:
    """
    Validate that a psutil.Process is the Ollama server — not just any process
    that happens to have inherited or reused the PID.
    Must match on name OR exe path, and cmdline must include 'serve'.
    """
    try:
        name = (proc.name() or "").lower()
        exe = (proc.exe() or "").lower()
        cmdline = proc.cmdline()

        name_match = "ollama" in name or "ollama" in exe
        serve_match = any("serve" in arg.lower() for arg in cmdline)

        return name_match and serve_match
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
