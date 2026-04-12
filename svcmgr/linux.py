"""
Linux platform service manager for Ollama.

Detection order for stop/start:
  1. systemd user service (systemctl --user) — no root required
  2. Spawn / signal directly — system service or bare process
     stop: returns False so psutil fallback in ollama.py takes over
     start: spawns `ollama serve` directly

The official Ollama Linux installer creates a *system* service
(ollama.service run by the ollama OS user), which requires root to
control.  llm-valet never runs as root, so if only the system service
exists, stop() returns False and ollama.py's psutil fallback handles
termination.  start() falls back to spawning `ollama serve` in the
background under the current user.

All subprocess calls use shell=False.
"""
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_OLLAMA_BINARY = "ollama"
_USER_UNIT     = "ollama.service"
# Common install locations beyond PATH
_KNOWN_PATHS   = [
    Path("/usr/local/bin/ollama"),
    Path("/usr/bin/ollama"),
    Path(os.path.expanduser("~/.local/bin/ollama")),
]


# ── Public interface ──────────────────────────────────────────────────────────

def start_service() -> bool:
    """Start Ollama via systemd user service or direct spawn."""
    if _has_user_unit():
        return _systemctl_user("start")
    return _spawn_serve()


def stop_service() -> bool:
    """
    Stop Ollama.

    Returns True only when we successfully stopped it via systemd --user.
    Returns False when the system service is in play (no root) or when
    ollama is running as a bare process — ollama.py's psutil fallback
    sends SIGTERM in that case.
    """
    if _has_user_unit():
        return _systemctl_user("stop")
    logger.info(
        "linux svcmgr: no user-level systemd unit found — "
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


# ── Systemd user unit ─────────────────────────────────────────────────────────

def _has_user_unit() -> bool:
    """Return True if a user-level systemd ollama unit is enabled or exists."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "cat", _USER_UNIT],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _systemctl_user(action: str) -> bool:
    """Run `systemctl --user <action> ollama.service`."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", action, _USER_UNIT],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("systemctl --user succeeded", extra={"action": action})
            return True

        # stop returns non-zero if the unit isn't running — treat as success
        if action == "stop" and "not loaded" in result.stderr:
            logger.info("systemctl --user stop — unit was not running")
            return True

        logger.error(
            "systemctl --user failed",
            extra={"action": action, "stderr": result.stderr.strip()},
        )
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.error("systemctl error", extra={"action": action, "error": str(exc)})
        return False


# ── Direct spawn fallback ─────────────────────────────────────────────────────

def _spawn_serve() -> bool:
    """
    Launch `ollama serve` as a detached background process.

    Used when there is no systemd user unit to manage.  The process is
    detached from our session (start_new_session=True) so it survives
    if llm-valet is restarted.
    """
    binary = _find_binary()
    if not binary:
        logger.error("ollama binary not found in PATH or known locations — cannot start")
        return False
    try:
        subprocess.Popen(  # noqa: S603  # shell=False, no user input in args
            [str(binary), "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("ollama serve spawned directly", extra={"binary": str(binary)})
        return True
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.error("failed to spawn ollama serve", extra={"error": str(exc)})
        return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_binary() -> Path | None:
    """Return path to the ollama binary, or None if not found."""
    in_path = shutil.which(_OLLAMA_BINARY)
    if in_path:
        return Path(in_path)
    for p in _KNOWN_PATHS:
        if p.is_file():
            return p
    return None
