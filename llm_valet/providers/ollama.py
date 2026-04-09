import asyncio
import logging
import re
import sys
from typing import Any

import httpx
import psutil

from llm_valet.providers.base import LLMProvider, ModelInfo, ProviderStatus

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
            # stream=False ensures Ollama sends a single complete response rather
            # than a chunked stream — required for keep_alive to be committed before
            # the connection closes.  Longer timeout for slow storage.
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    json={"model": model, "keep_alive": -1, "stream": False},
                )
                resp.raise_for_status()
                logger.info("model pre-warmed", extra={"model": model})
                return True
        except httpx.HTTPError as exc:
            logger.error("resume request failed", extra={"error": str(exc)})
            return False

    async def start(self) -> bool:
        """
        Start the Ollama service via platform service manager, then wait for it
        to become healthy.
        """
        if await self.health_check():
            logger.info("start called but Ollama is already running")
            return True

        started = _svcmgr_start()
        if not started:
            logger.error("svcmgr start_service() failed")
            return False

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
          2. svcmgr.stop_service() — platform-aware stop (launchctl bootout on
             macOS brew, osascript on macOS app, psutil SIGTERM fallback)
          3. Poll health_check() until False (30s timeout)
          4. psutil SIGKILL if still alive after svcmgr
        """
        await self.pause()

        stopped = _svcmgr_stop()
        if not stopped:
            logger.warning("svcmgr stop_service() returned False — trying psutil fallback")

        # Poll to confirm Ollama is down
        for _ in range(_SIGTERM_TIMEOUT_S // _POLL_INTERVAL_S):
            await asyncio.sleep(_POLL_INTERVAL_S)
            if not await self.health_check():
                logger.info("Ollama stopped")
                return True

        # Last resort: psutil SIGKILL (handles manually-started Ollama not managed by launchd)
        proc = _find_ollama_process()
        if proc is None:
            return not await self.health_check()

        logger.warning("Ollama still running — escalating to SIGKILL", extra={"pid": proc.pid})
        try:
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
            size_vram_mb = (int(loaded.get("size_vram") or 0)) // (1024 * 1024) or None
            return ProviderStatus(
                running=True,
                model_loaded=True,
                model_name=str(loaded.get("name", "")) or None,
                memory_used_mb=size_mb,
                size_vram_mb=size_vram_mb,
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

    async def list_models(self) -> list[ModelInfo]:
        """Return all locally available models with context_length from /api/tags + /api/show."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                raw_models: list[dict[str, Any]] = resp.json().get("models", [])

            results: list[ModelInfo] = []
            for m in raw_models:
                name = str(m.get("name", ""))
                if not name:
                    continue
                size_mb = (int(m.get("size") or 0)) // (1024 * 1024)
                ctx = await self._fetch_context_length(name)
                results.append(ModelInfo(name=name, size_mb=size_mb, context_length=ctx))
            return results
        except httpx.HTTPError as exc:
            logger.error("list_models request failed", extra={"error": str(exc)})
            return []

    async def _fetch_context_length(self, model_name: str) -> int | None:
        """Call /api/show and extract context_length from model_info."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/api/show",
                    json={"name": model_name},
                )
                resp.raise_for_status()
                model_info: dict[str, Any] = resp.json().get("model_info") or {}
                for key, val in model_info.items():
                    if key.endswith(".context_length") and isinstance(val, int):
                        return val
        except httpx.HTTPError:
            pass
        return None

    async def load_model(self, model_name: str) -> bool:
        """
        Switch to a different model:
          1. Validate name against allowlist regex.
          2. Unload the currently loaded model (if any) via keep_alive=0.
          3. Pre-warm the new model via keep_alive=-1.
          4. Update _model_name so future pause/resume use the new model.
        """
        if not _MODEL_NAME_RE.match(model_name):
            logger.error("load_model rejected — invalid model name", extra={"model": model_name})
            return False

        # Unload current model first (ignore failure — it may not be loaded)
        current = await self._resolve_model()
        if current and current != model_name:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        f"{self._base_url}/api/generate",
                        json={"model": current, "keep_alive": 0, "stream": False},
                    )
                    resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("load_model: unload current failed", extra={"error": str(exc)})

        # Pre-warm the new model.  stream=False ensures keep_alive is committed before
        # the connection closes.  60s timeout for slow storage.
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    json={"model": model_name, "keep_alive": -1, "stream": False},
                )
                resp.raise_for_status()
            self._model_name = model_name
            self._last_loaded_model = model_name
            logger.info("model loaded", extra={"model": model_name})
            return True
        except httpx.HTTPError as exc:
            logger.error("load_model: pre-warm failed", extra={"error": str(exc)})
            return False

    async def delete_model(self, model_name: str) -> bool:
        """Delete a model from local storage via DELETE /api/delete."""
        if not _MODEL_NAME_RE.match(model_name):
            logger.error("delete_model rejected — invalid model name", extra={"model": model_name})
            return False
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(
                    "DELETE",
                    f"{self._base_url}/api/delete",
                    json={"model": model_name},
                )
                resp.raise_for_status()
                logger.info("model deleted", extra={"model": model_name})
                return True
        except httpx.HTTPError as exc:
            logger.error("delete_model request failed", extra={"error": str(exc)})
            return False

    async def pull_model(self, model_name: str) -> bool:
        """Pull (download) a model via POST /api/pull. Blocks until complete."""
        if not _MODEL_NAME_RE.match(model_name):
            logger.error("pull_model rejected — invalid model name", extra={"model": model_name})
            return False
        try:
            logger.info("pulling model", extra={"model": model_name})
            async with httpx.AsyncClient(timeout=600.0) as client:
                resp = await client.post(
                    f"{self._base_url}/api/pull",
                    json={"model": model_name, "stream": False},
                )
                resp.raise_for_status()
                logger.info("model pull complete", extra={"model": model_name})
                return True
        except httpx.HTTPError as exc:
            logger.error("pull_model request failed", extra={"error": str(exc)})
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

# ── Platform service manager shims ───────────────────────────────────────────

def _svcmgr_start() -> bool:
    """Start Ollama via the platform service manager. Returns True on success."""
    if sys.platform == "darwin":
        try:
            from svcmgr.macos import start_service
            return start_service()
        except Exception as exc:
            logger.warning("svcmgr.start_service unavailable", extra={"error": str(exc)})
    # Linux/Windows: no svcmgr wired yet — log and let health-check loop handle it
    logger.info("no svcmgr for this platform — assuming Ollama will start externally")
    return True


def _svcmgr_stop() -> bool:
    """Stop Ollama via the platform service manager. Returns True on success."""
    if sys.platform == "darwin":
        try:
            from svcmgr.macos import stop_service
            return stop_service()
        except Exception as exc:
            logger.warning("svcmgr.stop_service unavailable", extra={"error": str(exc)})
    logger.info("no svcmgr for this platform — relying on psutil fallback")
    return False


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
