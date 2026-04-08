import asyncio
import logging
import logging.handlers
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from llm_valet.config import Settings, load_settings
from llm_valet.providers.base import LLMProvider, ProviderStatus
from llm_valet.providers.ollama import OllamaProvider
from llm_valet.resources.base import ResourceCollector, SystemMetrics
from llm_valet.watchdog import Watchdog

logger = logging.getLogger(__name__)


# ── Startup guards ────────────────────────────────────────────────────────────

def _check_not_root() -> None:
    if hasattr(os, "getuid") and os.getuid() == 0:
        sys.exit("llm-valet must not run as root — exiting")


def _configure_logging(settings: Settings) -> None:
    log_path = Path(settings.log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    json_formatter = logging.Formatter(
        '{"time": "%(asctime)s", "level": "%(levelname)s",'
        ' "logger": "%(name)s", "msg": "%(message)s"}'
    )

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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        logger.info("llm-valet starting", extra={"host": settings.host, "port": settings.port})
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
        version="0.1.0",
        lifespan=lifespan,
    )

    # ── Security middleware ───────────────────────────────────────────────────

    allowed_hosts = ["localhost", "127.0.0.1", "*.local", *settings.extra_allowed_hosts]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),  # empty by default — never "*"
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["X-API-Key"],
    )

    # ── Static files ──────────────────────────────────────────────────────────

    static_dir = Path(__file__).parent.parent / "static"
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
            if x_api_key != settings.api_key:
                raise HTTPException(status_code=401, detail="Unauthorized")

    Auth = Annotated[None, Depends(require_api_key)]

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    async def index() -> Any:
        index_file = static_dir / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return {"service": "llm-valet", "docs": "/docs"}

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
        return {
            "provider": provider_status.__dict__,
            "metrics": _metrics_to_dict(metrics),
            "watchdog": {"state": w.state.value},
        }

    @app.get("/watchdog")
    async def get_watchdog_status(
        _: Auth,
        w: Annotated[Watchdog, Depends(get_watchdog)],
    ) -> dict[str, Any]:
        """Current watchdog state machine state."""
        return {"state": w.state.value}

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
        success = await p.pause()
        if success:
            w.notify_manual_pause()
        return {"ok": success, "action": "pause"}

    @app.post("/resume")
    async def post_resume(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
        w: Annotated[Watchdog, Depends(get_watchdog)],
    ) -> dict[str, Any]:
        """Manual resume — pre-warm model into memory."""
        success = await p.resume()
        if success:
            w.notify_manual_resume()
        return {"ok": success, "action": "resume"}

    @app.post("/start")
    async def post_start(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
    ) -> dict[str, Any]:
        """Full service start."""
        success = await p.start()
        return {"ok": success, "action": "start"}

    @app.post("/stop")
    async def post_stop(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
    ) -> dict[str, Any]:
        """Graceful service shutdown — unload model, then stop service."""
        success = await p.stop()
        return {"ok": success, "action": "stop"}

    @app.post("/restart")
    async def post_restart(
        _: Auth,
        p: Annotated[LLMProvider, Depends(get_provider)],
    ) -> dict[str, Any]:
        """stop → 2s delay → start."""
        stop_ok = await p.stop()
        await asyncio.sleep(2)
        start_ok = await p.start()
        return {"ok": stop_ok and start_ok, "action": "restart"}

    @app.get("/config")
    async def get_config(_: Auth) -> dict[str, Any]:
        """Read current thresholds and watchdog settings."""
        return settings.thresholds.__dict__ | {
            "check_interval_seconds": settings.thresholds.check_interval_seconds
        }

    @app.put("/config")
    async def put_config(
        _: Auth,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Update thresholds at runtime and persist to config.yaml."""
        updated = settings.update_thresholds(body)
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
