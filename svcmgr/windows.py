"""
Windows platform service manager for Ollama.

Detection order for stop/start:
  1. Windows Service named "Ollama" (sc.exe) — if the service exists
  2. Launcher executable — start: launch ollama app from LOCALAPPDATA;
     stop: returns False so psutil fallback in ollama.py takes over

The official Ollama Windows installer installs to:
  %LOCALAPPDATA%\\Programs\\Ollama\\ollama.exe

It is NOT registered as a Windows Service by default — it runs as a
background tray application.  The Windows Service path is included for
environments that register it manually (e.g. enterprise deployments).

All subprocess calls use shell=False.
"""
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Executable installed by the official Ollama Windows installer
_LOCALAPPDATA     = Path(os.environ.get("LOCALAPPDATA", "~")).expanduser()
_OLLAMA_EXE       = _LOCALAPPDATA / "Programs" / "Ollama" / "ollama.exe"
_OLLAMA_BINARY    = "ollama"

# Windows Service name — only present in manual/enterprise setups
_SERVICE_NAME     = "Ollama"


# ── Public interface ──────────────────────────────────────────────────────────

def start_service() -> bool:
    """Start Ollama via Windows Service (if registered) or direct launch."""
    if _service_exists():
        return _sc("start")
    return _launch_exe()


def stop_service() -> bool:
    """
    Stop Ollama.

    Returns True only when we successfully stopped a Windows Service.
    Returns False for tray-app installs — ollama.py's psutil fallback
    sends SIGTERM in that case.
    """
    if _service_exists():
        return _sc("stop")
    logger.info(
        "windows svcmgr: no Ollama Windows Service found — "
        "psutil fallback will handle stop"
    )
    return False


def restart_service() -> bool:
    ok = stop_service()
    if not ok:
        logger.warning("stop_service returned False during restart — attempting start anyway")
    return start_service()


def is_installed() -> bool:
    return _find_binary() is not None


# ── Windows Service (sc.exe) ──────────────────────────────────────────────────

def _service_exists() -> bool:
    """Return True if a Windows Service named 'Ollama' is registered."""
    try:
        result = subprocess.run(
            ["sc", "query", _SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _sc(action: str) -> bool:
    """Run `sc <action> Ollama`."""
    try:
        result = subprocess.run(
            ["sc", action, _SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info("sc.exe succeeded", extra={"action": action})
            return True

        # sc stop returns non-zero if already stopped — treat as success
        if action == "stop" and "1062" in result.stdout:
            # Error 1062: The service has not been started
            logger.info("sc stop — service was already stopped")
            return True

        logger.error(
            "sc.exe failed",
            extra={"action": action, "stdout": result.stdout.strip()},
        )
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.error("sc.exe error", extra={"action": action, "error": str(exc)})
        return False


# ── Direct launch fallback ────────────────────────────────────────────────────

def _launch_exe() -> bool:
    """
    Launch the Ollama executable directly (tray-app / no service).

    Uses DETACHED_PROCESS | CREATE_NO_WINDOW so the child process
    survives if llm-valet's console window is closed.
    """
    binary = _find_binary()
    if not binary:
        logger.error("ollama.exe not found — cannot start service")
        return False

    try:
        # DETACHED_PROCESS (0x00000008) + CREATE_NO_WINDOW (0x08000000)
        # Keeps the child alive independently of our console, no visible window.
        DETACHED_PROCESS   = 0x00000008
        CREATE_NO_WINDOW   = 0x08000000
        subprocess.Popen(
            [str(binary)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
        )
        logger.info("ollama.exe launched directly", extra={"path": str(binary)})
        return True
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.error("failed to launch ollama.exe", extra={"error": str(exc)})
        return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_binary() -> Path | None:
    """Return path to the ollama binary, checking known locations and PATH."""
    if _OLLAMA_EXE.is_file():
        return _OLLAMA_EXE
    in_path = shutil.which(_OLLAMA_BINARY)
    if in_path:
        return Path(in_path)
    return None
