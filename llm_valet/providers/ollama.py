import asyncio
import logging
import re
import sys
import types
from typing import Any, cast

import httpx
import psutil

from llm_valet.providers.base import LLMProvider, ModelInfo, ProviderStatus

logger = logging.getLogger(__name__)

# Model name validation — must match before any subprocess/API use.
# Length cap (200) guards against DoS via oversized strings in logs/API calls.
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9:._-]{1,200}$")

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
        # Context window cached at pause time — Ollama resets to its default on
        # resume if options are not re-applied.  /api/ps is empty after eviction
        # so this must be captured before the keep_alive=0 call.
        self._last_loaded_ctx: int | None = None
        # Prevents concurrent load_model() calls from interleaving their
        # unload/load sequences and leaving _model_name in an inconsistent state.
        self._load_lock = asyncio.Lock()

    # ── LLMProvider interface ─────────────────────────────────────────────────

    async def pause(self) -> bool:
        """Evict the loaded model from memory via keep_alive=0. Service stays running."""
        # Capture context_length before eviction — /api/ps is empty after eviction,
        # so resume() would have no way to recover the active context window.
        current_status = await self.status()
        self._last_loaded_ctx = current_status.loaded_context_length

        model = await self._resolve_model()
        if model is None:
            logger.info("pause skipped — no model currently loaded")
            return True

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    # stream=False required: without it Ollama sends a chunked response
                    # and resp.json() only parses the first chunk, so done_reason=="unload"
                    # is never seen and pause() silently returns False.
                    json={"model": model, "keep_alive": 0, "stream": False},
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
            payload: dict[str, object] = {"model": model, "keep_alive": -1, "stream": False}
            if self._last_loaded_ctx is not None:
                # Restore the context window that was active before eviction.
                # Without this, Ollama resets to its default on the next load.
                payload["options"] = {"num_ctx": self._last_loaded_ctx}
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    json=payload,
                )
                resp.raise_for_status()
                logger.info(
                    "model pre-warmed",
                    extra={"model": model, "num_ctx": self._last_loaded_ctx},
                )
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
            loaded_ctx = loaded.get("context_length") or None
            return ProviderStatus(
                running=True,
                model_loaded=True,
                model_name=str(loaded.get("name", "")) or None,
                memory_used_mb=size_mb,
                size_vram_mb=size_vram_mb,
                loaded_context_length=int(loaded_ctx) if loaded_ctx else None,
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

    async def load_model(self, model_name: str, num_ctx: int | None = None) -> bool:
        """
        Switch to a different model:
          1. Validate name against allowlist regex.
          2. Unload the currently loaded model (if any) via keep_alive=0.
          3. Pre-warm the new model via keep_alive=-1, optionally with num_ctx.
          4. Update _model_name so future pause/resume use the new model.
        num_ctx overrides Ollama's default context window for this load.
        Must be >= 512 if provided; silently ignored if out of range.
        Serialised by _load_lock — concurrent /load calls are queued, not raced.
        """
        async with self._load_lock:
            return await self._load_model_locked(model_name, num_ctx)

    async def _load_model_locked(self, model_name: str, num_ctx: int | None = None) -> bool:
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
        payload: dict[str, object] = {"model": model_name, "keep_alive": -1, "stream": False}
        if num_ctx is not None and num_ctx >= 512:
            payload["options"] = {"num_ctx": num_ctx}
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    json=payload,
                )
                resp.raise_for_status()
            self._model_name = model_name
            self._last_loaded_model = model_name
            logger.info("model loaded", extra={"model": model_name, "num_ctx": num_ctx})
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

    async def force_pause(self) -> bool:
        """
        Force-evict the model by killing ollama runner subprocesses directly.

        Used when pause() is blocked by an active inference request — the runner
        process is the Ollama subprocess that handles model inference; killing it
        interrupts the inference without stopping the Ollama service itself.

        Captures model metadata before killing so resume() can restore state.
        Falls back to regular pause() if no runner processes are found.
        """
        # Capture model info while /api/ps is still populated.
        # After the runner is killed, /api/ps returns empty.
        current = await self.status()
        if current.model_name:
            self._last_loaded_model = current.model_name
        self._last_loaded_ctx = current.loaded_context_length

        killed = _kill_ollama_runners()
        if killed > 0:
            logger.info("force_pause: killed runner processes", extra={"count": killed})
            # Brief pause for Ollama to update its internal state before callers poll /api/ps
            await asyncio.sleep(0.5)
            return True

        # No runner processes found — model may already be unloaded or not using a
        # runner subprocess on this platform; fall back to regular keep_alive=0 eviction.
        logger.info("force_pause: no runner processes found — falling back to pause()")
        return await self.pause()

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
    _mod = _svcmgr_module()
    if _mod is None:
        logger.info("no svcmgr available for this platform — assuming Ollama will start externally")
        return True
    try:
        return cast(bool, _mod.start_service())
    except Exception as exc:
        logger.warning("svcmgr.start_service raised", extra={"error": str(exc)})
        return True  # let health-check loop determine actual state


def _svcmgr_stop() -> bool:
    """Stop Ollama via the platform service manager. Returns True on success."""
    _mod = _svcmgr_module()
    if _mod is None:
        logger.info("no svcmgr available for this platform — relying on psutil fallback")
        return False
    try:
        return cast(bool, _mod.stop_service())
    except Exception as exc:
        logger.warning("svcmgr.stop_service raised", extra={"error": str(exc)})
        return False  # psutil fallback will take over


def _svcmgr_module() -> types.ModuleType | None:
    """
    Return the platform-appropriate svcmgr module, or None if unavailable.

    Importing here (not at module top) keeps Linux/Windows imports out of
    macOS's namespace and vice versa — each module uses platform-only stdlib.
    """
    try:
        if sys.platform == "darwin":
            import svcmgr.macos as _m

            return _m
        if sys.platform == "linux":
            import svcmgr.linux as _m

            return _m
        if sys.platform == "win32":
            import svcmgr.windows as _m

            return _m
    except ImportError as exc:
        logger.warning("svcmgr module import failed", extra={"error": str(exc)})
    return None


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


def _is_ollama_runner(proc: psutil.Process) -> bool:
    """
    Validate that a process is an Ollama inference runner, not the server.

    Runner processes are spawned by Ollama to serve a loaded model.  Killing
    them interrupts active inference without stopping the Ollama service.

    Detection covers two patterns:
      - Binary named 'ollama_llama_runner' (macOS App bundle / Windows .exe)
      - Ollama binary invoked with a 'runner' subcommand (some Linux installs)
        but NOT with 'serve' — that would be the server itself.
    """
    try:
        name = (proc.name() or "").lower()
        exe = (proc.exe() or "").lower()
        cmdline = proc.cmdline()
        if "ollama_llama_runner" in name or "ollama_llama_runner" in exe:
            return True
        # Ollama binary as runner subcommand — must not be the serve process
        if "ollama" in exe and any("runner" in arg.lower() for arg in cmdline):
            if not any("serve" in arg.lower() for arg in cmdline):
                return True
        return False
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _kill_ollama_runners() -> int:
    """
    Kill ollama runner subprocesses. Returns the count of processes killed.
    Uses psutil.kill() — no shell, no subprocess, no injection surface.
    """
    killed = 0
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if _is_ollama_runner(proc):
                proc.kill()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


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
