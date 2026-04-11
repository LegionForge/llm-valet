"""
macOS platform service manager for Ollama.

Handles two Ollama install variants:
  App:      /Applications/Ollama.app  (menu-bar app, no plist needed)
  Brew CLI: ~/Library/LaunchAgents/com.ollama.ollama.plist

Detection order: App first, then Brew CLI plist.
All subprocess calls use shell=False with explicit argument lists.
"""
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Ollama install paths ──────────────────────────────────────────────────────

_APP_BUNDLE     = Path("/Applications/Ollama.app")
_APP_EXECUTABLE = _APP_BUNDLE / "Contents/MacOS/Ollama"

# Brew formula plist names — older versions used com.ollama.ollama, newer
# homebrew formula uses homebrew.mxcl.ollama.  Check both.
_BREW_VARIANTS = [
    ("homebrew.mxcl.ollama",
     Path("~/Library/LaunchAgents/homebrew.mxcl.ollama.plist").expanduser()),
    ("com.ollama.ollama",
     Path("~/Library/LaunchAgents/com.ollama.ollama.plist").expanduser()),
]


# ── Public interface ──────────────────────────────────────────────────────────

def start_service() -> bool:
    """Start Ollama via the appropriate mechanism for the installed variant."""
    variant = _detect_variant()
    if variant == "app":
        return _open_app()
    if variant == "brew":
        label, plist = _brew_plist()
        return _launchctl("bootstrap", label, plist=plist)
    logger.error("Ollama not found — cannot start service")
    return False


def stop_service() -> bool:
    """Stop Ollama and prevent automatic respawn."""
    variant = _detect_variant()
    if variant == "app":
        return _quit_app()
    if variant == "brew":
        label, _ = _brew_plist()
        return _launchctl("bootout", label)
    logger.warning("Ollama not found — stop_service is a no-op")
    return True


def restart_service() -> bool:
    ok = stop_service()
    if not ok:
        logger.warning("stop_service returned False during restart — attempting start anyway")
    return start_service()


def is_installed() -> bool:
    return _detect_variant() is not None


# ── Variant detection ─────────────────────────────────────────────────────────

def _brew_plist() -> tuple[str, Path]:
    """Return (label, plist_path) for whichever brew plist variant is present."""
    for label, plist in _BREW_VARIANTS:
        if plist.is_file():
            return label, plist
    # Fall back to first entry — launchctl will error meaningfully
    return _BREW_VARIANTS[0]


def _detect_variant() -> str | None:
    """Return 'app', 'brew', or None."""
    if _APP_BUNDLE.is_dir():
        return "app"
    for _, plist in _BREW_VARIANTS:
        if plist.is_file():
            return "brew"
    return None


# ── App variant (menu-bar app) ────────────────────────────────────────────────

def _open_app() -> bool:
    """Launch Ollama.app via `open -a`."""
    try:
        result = subprocess.run(
            ["open", "-a", "Ollama"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            logger.info("Ollama.app launched via open -a")
            return True
        logger.error("open -a Ollama failed", extra={"stderr": result.stderr.strip()})
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.error("open -a Ollama error", extra={"error": str(exc)})
        return False


def _quit_app() -> bool:
    """
    Quit Ollama.app via AppleScript.
    Falls back to direct executable termination if AppleScript fails.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", 'quit app "Ollama"'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            logger.info("Ollama.app quit via osascript")
            return True
        logger.warning(
            "osascript quit failed — falling back to process termination",
            extra={"stderr": result.stderr.strip()},
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("osascript unavailable", extra={"error": str(exc)})

    # Fallback: terminate via executable path (no shell, validated path)
    return _terminate_app_executable()


def _terminate_app_executable() -> bool:
    """Send SIGTERM to the Ollama.app executable process by matching its exe path."""
    import psutil

    target_exe = str(_APP_EXECUTABLE).lower()
    for proc in psutil.process_iter(["exe"]):
        try:
            exe = (proc.info.get("exe") or "").lower()
            if exe == target_exe:
                proc.terminate()
                logger.info("Ollama.app process terminated", extra={"pid": proc.pid})
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    logger.warning("Ollama.app process not found for termination")
    return False


# ── Brew CLI variant (launchctl) ──────────────────────────────────────────────

def _launchctl(action: str, label: str, plist: Path | None = None) -> bool:
    """
    Run a launchctl command for user-level agents.

    bootstrap domain/plist  — load and start the agent
    bootout   domain/label  — stop the agent and remove from launchd

    User domain is gui/<uid> — never requires root.
    """
    import os

    uid = os.getuid()  # type: ignore[attr-defined]  # not in Windows stubs; file is macOS-only
    domain = f"gui/{uid}"

    if action == "bootstrap":
        if plist is None or not plist.is_file():
            logger.error("launchctl bootstrap: plist not found", extra={"path": str(plist)})
            return False
        cmd = ["launchctl", "bootstrap", domain, str(plist)]

    elif action == "bootout":
        cmd = ["launchctl", "bootout", f"{domain}/{label}"]

    else:
        logger.error("unknown launchctl action", extra={"action": action})
        return False

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            logger.info("launchctl succeeded", extra={"action": action, "label": label})
            return True

        # launchctl bootout returns non-zero if the service isn't loaded — treat as success
        if action == "bootout" and "No such process" in result.stderr:
            logger.info("launchctl bootout — service was not running", extra={"label": label})
            return True

        logger.error(
            "launchctl failed",
            extra={"action": action, "label": label, "stderr": result.stderr.strip()},
        )
        return False

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.error("launchctl error", extra={"action": action, "error": str(exc)})
        return False
