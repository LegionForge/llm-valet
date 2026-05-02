import asyncio
import hmac
import ipaddress
import logging
import logging.handlers
import os
import re
import socket
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint

from llm_valet.config import Settings, load_settings
from llm_valet.providers.base import LLMProvider, ModelInfo, ProviderStatus
from llm_valet.providers.ollama import OllamaProvider
from llm_valet.resources.base import ResourceCollector, SystemMetrics
from llm_valet.watchdog import Watchdog

logger = logging.getLogger(__name__)

_VERSION = "0.6.0"

# T4 — model names are passed to Ollama CLI/API; only safe characters allowed.
# Prevents injection even though shell=False is enforced throughout.
# Length cap (200) guards against DoS via oversized strings in logs/API calls.
_MODEL_RE = re.compile(r"^[a-zA-Z0-9:._-]{1,200}$")

# Prevents memory-pressure DoS via enormous JSON bodies on mutation endpoints.
_MAX_BODY_BYTES = 64 * 1024  # 64 KB
# Minimum free disk space required before accepting a model pull request.
_MIN_FREE_MB = 5 * 1024  # 5 GB


def _validate_model_name(name: str) -> None:
    if not _MODEL_RE.match(name):
        raise HTTPException(status_code=422, detail="invalid model name")


class _RateLimiter:
    """
    Simple per-key time-based cooldown — prevents loop scripts from hammering
    destructive endpoints (/stop, /start, /restart, /models/pull).
    Single-worker only; not safe for multi-process deployments.
    """

    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def check(self, key: str, min_interval: float) -> None:
        """Raise HTTP 429 if key was called within min_interval seconds."""
        now = time.monotonic()
        elapsed = now - self._last.get(key, 0.0)
        if elapsed < min_interval:
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests — wait {min_interval - elapsed:.1f}s",
            )
        self._last[key] = now


# ── Startup guards ────────────────────────────────────────────────────────────


def _check_not_root() -> None:
    if hasattr(os, "getuid") and os.getuid() == 0:
        sys.exit("llm-valet must not run as root — exiting")


class _JsonFormatter(logging.Formatter):
    """JSON log formatter that captures extra={} fields into the output."""

    _SKIP = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)

    def format(self, record: logging.LogRecord) -> str:
        import json

        out: dict[str, object] = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key not in self._SKIP and not key.startswith("_"):
                out[key] = val
        return json.dumps(out)


def _configure_logging(settings: Settings) -> None:
    log_path = Path(settings.log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    json_formatter = _JsonFormatter()

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(json_formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(json_formatter)

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])

    # httpx logs every outgoing request at INFO — one per watchdog tick per
    # Ollama API call. Suppress to WARNING to keep the log readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Uvicorn's access log uses a plain-text format that breaks JSON log
    # parsing. Disable it — our /status endpoint serves the same information.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").propagate = False


# ── App factory ───────────────────────────────────────────────────────────────


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = load_settings()

    _check_not_root()
    _configure_logging(settings)

    provider = _build_provider(settings)
    collector = _build_collector(settings)
    watchdog = Watchdog(provider, collector, settings.thresholds)
    rate_limiter = _RateLimiter()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        logger.info("llm-valet starting", extra={"host": settings.host, "port": settings.port})
        # If a model is already loaded and its memory footprint exceeds the RAM pause
        # threshold, the watchdog's auto-pause will never fire — the threshold is already
        # breached before any additional load arrives.  Warn immediately so the user can
        # raise ram_pause_pct above the model's baseline.
        try:
            ps = await provider.status()
            if ps.model_loaded and ps.memory_used_mb is not None:
                metrics = collector.collect()
                threshold_mb = metrics.memory.total_mb * (settings.thresholds.ram_pause_pct / 100)
                if ps.memory_used_mb > threshold_mb:
                    used_pct = round(ps.memory_used_mb / metrics.memory.total_mb * 100, 1)
                    logger.warning(
                        "overcommit at startup — loaded model exceeds RAM pause threshold; "
                        "watchdog auto-pause will not trigger. "
                        "Raise ram_pause_pct above %.1f%% or unload the model.",
                        used_pct,
                        extra={
                            "model": ps.model_name,
                            "model_mb": ps.memory_used_mb,
                            "model_pct": used_pct,
                            "ram_pause_pct": settings.thresholds.ram_pause_pct,
                        },
                    )
        except Exception as exc:
            # provider.status() or collector.collect() raised — Ollama is not reachable
            # yet at startup. The if-checks above are never reached in this case.
            # Not an error: valet starts regardless and the watchdog will retry on its
            # normal interval once Ollama comes up.
            logger.debug("startup overcommit check skipped (provider unreachable): %s", exc)
        watchdog_task = asyncio.create_task(watchdog.run(), name="watchdog")
        try:
            yield
        finally:
            await watchdog.stop()
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
            logger.info("llm-valet shutting down")

    app = FastAPI(
        title="llm-valet",
        description="LLM lifecycle manager — pause/resume based on resource pressure and gaming detection",  # noqa: E501
        version=_VERSION,
        lifespan=lifespan,
    )

    # ── Security middleware ───────────────────────────────────────────────────

    # T2 — DNS rebinding: reject any Host header not in the allowlist.
    # Without this a malicious page can rebind DNS to 127.0.0.1 and reach the API.
    allowed_hosts = ["localhost", "127.0.0.1", "*.local", *settings.extra_allowed_hosts]
    # SECURITY EXCEPTION: S104 — intentional 0.0.0.0 string comparison (not a bind call).
    # Mitigations: (1) X-API-Key required for all non-localhost requests; (2) TrustedHostMiddleware
    # blocks DNS rebinding; (3) user must opt in via config (default is 127.0.0.1).
    # Reviewed: JP Cruz (jp@legionforge.org), 2026-04-16
    if settings.host == "0.0.0.0":  # noqa: S104  # nosec B104
        # Raw IP addresses in the Host header are not a DNS rebinding vector —
        # rebinding requires a domain name. Auto-allow local interface IPs so the
        # WebUI is reachable by IP immediately after LAN install without manual config.
        import psutil

        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    allowed_hosts.append(addr.address)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    # Body size limit — rejects oversized JSON bodies before they are parsed.
    # 64 KB is well above any legitimate PUT /config payload (~200 bytes).
    # Content-Length alone is insufficient — chunked transfer encoding omits it.
    @app.middleware("http")
    async def limit_body_size(request: Request, call_next: RequestResponseEndpoint) -> Response:
        cl = request.headers.get("content-length")
        if cl and int(cl) > _MAX_BODY_BYTES:
            return Response("Request body too large", status_code=413)
        # Drain and buffer to catch chunked requests that omit Content-Length.
        # Setting request._body re-injects the buffered body for downstream handlers:
        # Starlette 1.x _CachedRequest.wrapped_receive() checks _body before
        # _stream_consumed, so the downstream app still reads the correct body
        # even though we consumed the stream here.
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > _MAX_BODY_BYTES:
                return Response("Request body too large", status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)  # _body checked before _stream_consumed in wrapped_receive
        return await call_next(request)

    # T3 — CORS wildcard prevention: allow_origins is config-only, never "*".
    # Cross-origin JS cannot reach the API unless an explicit origin is listed in config.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),  # empty by default — never "*"
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["X-API-Key"],
    )

    # ── Static files ──────────────────────────────────────────────────────────

    # static/ lives inside the package so it is included in pip/git installs.
    # parent.parent pointed at the repo root in dev but missed site-packages installs.
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # ── Dependency helpers ────────────────────────────────────────────────────

    def get_provider() -> LLMProvider:
        return provider

    def get_collector() -> ResourceCollector:
        return collector

    def get_watchdog() -> Watchdog:
        return watchdog

    async def require_api_key(
        request: Request,
        x_api_key: Annotated[str, Header()] = "",
    ) -> None:
        """
        No auth required for 127.0.0.1 (localhost).
        X-API-Key required for all other origins when host is 0.0.0.0.
        """
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "::1"):
            if not settings.api_key:
                raise HTTPException(status_code=403, detail="LAN access requires api_key in config")
            if not hmac.compare_digest(x_api_key, settings.api_key):
                raise HTTPException(status_code=401, detail="Unauthorized")

    Auth = Annotated[None, Depends(require_api_key)]

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        index_file = static_dir / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return {"service": "llm-valet", "docs": "/docs"}

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Any:
        favicon_file = static_dir / "favicon.ico"
        if favicon_file.is_file():
            return FileResponse(favicon_file, media_type="image/x-icon")
        return Response(status_code=404)

    def _is_local(request: Request) -> bool:
        client = request.client
        return client is not None and client.host in ("127.0.0.1", "::1")

    @app.get("/setup", include_in_schema=False)
    async def setup_status(request: Request) -> Any:
        # Only expose the key to localhost — LAN clients learn only whether setup is needed.
        # Once acknowledged the key is never returned again regardless of origin.
        if not settings.key_acknowledged and _is_local(request):
            return {"needs_setup": True, "api_key": settings.api_key}
        return {"needs_setup": not settings.key_acknowledged, "api_key": None}

    @app.post("/setup/acknowledge", include_in_schema=False)
    async def acknowledge_setup(request: Request) -> Any:
        # Localhost-only — prevents a LAN client from prematurely dismissing the modal.
        if not _is_local(request):
            raise HTTPException(
                status_code=403, detail="Setup acknowledgment requires local access"
            )
        settings.acknowledge_key()
        return {"ok": True}

    @app.post("/setup/apply", include_in_schema=False)
    async def apply_setup(request: Request) -> Any:
        # Localhost-only — network config changes require physical/local access.
        if not _is_local(request):
            raise HTTPException(status_code=403, detail="Setup requires local access")
        body = await request.json()
        host = str(body.get("host", "127.0.0.1")).strip()
        port = int(body.get("port", 8765))

        # Validate host — must be a known safe value or a valid IP
        # SECURITY EXCEPTION: S104 — string comparison against "0.0.0.0", not a bind call.
        # This is input validation: rejecting unknown values, not opening a socket.
        # Reviewed: JP Cruz (jp@legionforge.org), 2026-04-16
        if host not in ("127.0.0.1", "0.0.0.0"):  # noqa: S104  # nosec B104
            try:
                ipaddress.ip_address(host)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid host address") from exc

        if not (1024 <= port <= 65535):
            raise HTTPException(status_code=400, detail="Port must be between 1024 and 65535")

        settings.apply_network_config(host, port)

        # Browser redirect target — 0.0.0.0 binds all interfaces but browsers need a real host.
        # SECURITY EXCEPTION: S104 — string comparison only; no socket is opened here.
        # Reviewed: JP Cruz (jp@legionforge.org), 2026-04-16
        display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host  # noqa: S104  # nosec B104
        redirect_url = f"http://{display_host}:{port}/"

        # Trigger graceful restart via call_later so the HTTP response returns first.
        # launchd (macOS) and systemd (Linux) KeepAlive/Restart will respawn the process.
        loop = asyncio.get_event_loop()
        loop.call_later(1.0, lambda: os._exit(0))

        return {"ok": True, "redirect_url": redirect_url}

    @app.get("/status")
    async def get_status(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
        c: Annotated[ResourceCollector, Depends(get_collector)],
        w: Annotated[Watchdog, Depends(get_watchdog)],
    ) -> dict[str, Any]:
        """Provider state + current resource snapshot + watchdog state."""
        provider_status: ProviderStatus = await p.status()
        metrics: SystemMetrics = c.collect()
        # Over-commit: model's actual memory footprint (from Ollama /api/ps) exceeds
        # the RAM threshold psutil would need to trigger auto-pause.  psutil underreports
        # real memory commitment when the model spills into NVMe swap (Apple Silicon and
        # others), so the watchdog's RAM% check may never fire — the user needs a warning.
        ram_threshold_mb = metrics.memory.total_mb * (settings.thresholds.ram_pause_pct / 100)
        overcommit = bool(
            provider_status.model_loaded
            and provider_status.memory_used_mb is not None
            and provider_status.memory_used_mb > ram_threshold_mb
        )
        return {
            "provider": {
                **provider_status.__dict__,
                "endpoint": settings.ollama_url,
                "overcommit": overcommit,
            },
            "metrics": _metrics_to_dict(metrics),
            "watchdog": {"state": w.state.value, "last_reason": w.last_reason},
            "version": _VERSION,
            # Expose bind posture so the WebUI can warn when LAN-exposed without auth
            "security": {
                "lan_exposed": settings.host not in ("127.0.0.1", "::1", "localhost"),
                "auth_enabled": bool(settings.api_key),
            },
        }

    @app.get("/watchdog")
    async def get_watchdog_status(
        _: Auth,
        w: Annotated[Watchdog, Depends(get_watchdog)],
    ) -> dict[str, Any]:
        """Current watchdog state machine state and last transition reason."""
        return {"state": w.state.value, "last_reason": w.last_reason}

    @app.get("/metrics")
    async def get_metrics(
        _: Auth,
        c: Annotated[ResourceCollector, Depends(get_collector)],
    ) -> dict[str, Any]:
        """Live SystemMetrics from ResourceCollector."""
        return _metrics_to_dict(c.collect())

    @app.post("/pause")
    async def post_pause(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
        w: Annotated[Watchdog, Depends(get_watchdog)],
    ) -> dict[str, Any]:
        """Manual pause — unload model from memory."""
        rate_limiter.check("pause", 2.0)
        success = await p.pause()
        if success:
            w.notify_manual_pause()
        return {"ok": success, "action": "pause"}

    @app.post("/pause/force")
    async def post_pause_force(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
        w: Annotated[Watchdog, Depends(get_watchdog)],
    ) -> dict[str, Any]:
        """Force pause — kills inference runner process directly.
        Use when normal pause is blocked by an active inference request."""
        success = await p.force_pause()
        if success:
            w.notify_manual_pause()
        return {"ok": success, "action": "force_pause"}

    @app.post("/resume")
    async def post_resume(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
        w: Annotated[Watchdog, Depends(get_watchdog)],
    ) -> dict[str, Any]:
        """Manual resume — pre-warm model into memory."""
        rate_limiter.check("resume", 2.0)
        success = await p.resume()
        if success:
            w.notify_manual_resume()
        return {"ok": success, "action": "resume"}

    @app.get("/models")
    async def get_models(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
    ) -> dict[str, Any]:
        """List all locally available models."""
        models: list[ModelInfo] = await p.list_models()
        return {"models": [m.__dict__ for m in models]}

    @app.post("/load")
    async def post_load(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
        w: Annotated[Watchdog, Depends(get_watchdog)],
        request: Request,
    ) -> dict[str, Any]:
        """Load a specific model — unloads current model first if different."""
        body = await request.json()
        model_name = body.get("model", "")
        if not isinstance(model_name, str) or not model_name:
            raise HTTPException(status_code=422, detail="model field required")
        _validate_model_name(model_name)
        raw_ctx = body.get("num_ctx")
        num_ctx: int | None = None
        if raw_ctx is not None:
            if not isinstance(raw_ctx, int) or raw_ctx < 512:
                raise HTTPException(status_code=422, detail="num_ctx must be an integer >= 512")
            num_ctx = raw_ctx
        success = await p.load_model(model_name, num_ctx=num_ctx)
        if success:
            w.notify_manual_resume()
        return {"ok": success, "action": "load", "model": model_name, "num_ctx": num_ctx}

    @app.delete("/models/{model_name}")
    async def delete_model(
        _: Auth,
        model_name: str,
        p: Annotated[LLMProvider, Depends(get_provider)],
    ) -> dict[str, Any]:
        """Delete a locally stored model."""
        _validate_model_name(model_name)
        success = await p.delete_model(model_name)
        return {"ok": success, "action": "delete", "model": model_name}

    @app.post("/models/pull")
    async def pull_model(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
        c: Annotated[ResourceCollector, Depends(get_collector)],
        request: Request,
    ) -> dict[str, Any]:
        """Pull (download) a model from the registry. Blocks until complete."""
        rate_limiter.check("pull", 5.0)
        body = await request.json()
        model_name = body.get("model", "")
        if not isinstance(model_name, str) or not model_name:
            raise HTTPException(status_code=422, detail="model field required")
        _validate_model_name(model_name)
        # Guard against disk exhaustion — large models can fill the drive mid-download
        # and corrupt Ollama's model index. Require at least 5 GB free before starting.
        disk = c.collect().disk
        if disk.free_mb < _MIN_FREE_MB:
            raise HTTPException(
                status_code=507,
                detail=(
                    f"Insufficient disk space — "
                    f"{disk.free_mb} MB free, {_MIN_FREE_MB} MB required"
                ),
            )
        success = await p.pull_model(model_name)
        return {"ok": success, "action": "pull", "model": model_name}

    @app.post("/stop/force")
    async def post_stop_force(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        """Force stop — kills inference runner then stops the service.
        Returns immediately; poll /status for result."""
        # Reuse the /stop rate-limit key — both are destructive service operations
        rate_limiter.check("stop", 3.0)

        async def _force_stop() -> None:
            await p.force_pause()
            await p.stop()

        background_tasks.add_task(_force_stop)
        return {"ok": True, "action": "force_stop"}

    @app.post("/start")
    async def post_start(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        """Full service start — returns immediately; poll /status for result."""
        rate_limiter.check("start", 3.0)
        background_tasks.add_task(p.start)
        return {"ok": True, "action": "start"}

    @app.post("/stop")
    async def post_stop(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        """Graceful service shutdown — returns immediately; poll /status for result."""
        rate_limiter.check("stop", 3.0)
        background_tasks.add_task(p.stop)
        return {"ok": True, "action": "stop"}

    @app.post("/restart")
    async def post_restart(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        """stop → 2s delay → start — returns immediately; poll /status for result."""
        rate_limiter.check("restart", 3.0)

        async def _restart() -> None:
            await p.stop()
            await asyncio.sleep(2)
            await p.start()

        background_tasks.add_task(_restart)
        return {"ok": True, "action": "restart"}

    @app.get("/config")
    async def get_config(_: Auth) -> dict[str, Any]:
        """Read current thresholds and watchdog settings."""
        return dict(settings.thresholds.__dict__)

    @app.put("/config")
    async def put_config(
        _: Auth,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Update thresholds at runtime and persist to config.yaml."""
        try:
            updated = settings.update_thresholds(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "thresholds": updated}

    return app


# ── Provider / collector selection ───────────────────────────────────────────


def _build_provider(settings: Settings) -> LLMProvider:
    if settings.provider == "ollama":
        return OllamaProvider(
            base_url=settings.ollama_url,
            model_name=settings.model_name,
        )
    raise ValueError(f"Unknown provider: {settings.provider!r}")


def _build_collector(settings: Settings) -> ResourceCollector:
    if sys.platform == "darwin":
        from llm_valet.resources.macos import MacOSResourceCollector

        return MacOSResourceCollector()
    elif sys.platform == "linux":
        from llm_valet.resources.linux import LinuxResourceCollector

        return LinuxResourceCollector()
    else:
        from llm_valet.resources.windows import WindowsResourceCollector

        return WindowsResourceCollector()


# ── Serialisation helper ──────────────────────────────────────────────────────


def _metrics_to_dict(m: SystemMetrics) -> dict[str, Any]:
    return {
        "memory": {
            "total_mb": m.memory.total_mb,
            "used_mb": m.memory.used_mb,
            "used_pct": m.memory.used_pct,
            "pressure": m.memory.pressure.value,
        },
        "cpu": {
            "used_pct": m.cpu.used_pct,
            "core_count": m.cpu.core_count,
        },
        "gpu": {
            "available": m.gpu.available,
            "vram_total_mb": m.gpu.vram_total_mb,
            "vram_used_mb": m.gpu.vram_used_mb,
            "vram_used_pct": m.gpu.vram_used_pct,
            "compute_pct": m.gpu.compute_pct,
        },
        "disk": {
            "path": m.disk.path,
            "total_mb": m.disk.total_mb,
            "used_mb": m.disk.used_mb,
            "free_mb": m.disk.free_mb,
            "used_pct": m.disk.used_pct,
        },
        "timestamp": m.timestamp,
    }


# ── Entrypoint ────────────────────────────────────────────────────────────────

app = create_app()


def main() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "llm_valet.api:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
