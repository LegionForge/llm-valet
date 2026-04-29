import asyncio
import enum
import logging
import time

import psutil

from llm_valet.providers.base import LLMProvider
from llm_valet.resources.base import ResourceCollector, ResourceThresholds, ThresholdEngine

logger = logging.getLogger(__name__)


class WatchdogState(enum.Enum):
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    RESUMING = "resuming"
    PROVIDER_DOWN = "provider_down"


class Watchdog:
    """
    Combines game-process detection with resource collector signals.

    Holds references to a LLMProvider and a ResourceCollector — never calls
    psutil or any platform API directly for resource data.

    State machine: RUNNING → PAUSING → PAUSED → RESUMING → RUNNING
                   RUNNING / PAUSED → PROVIDER_DOWN → RUNNING

    Every state transition is logged with a structured reason string.
    """

    def __init__(
        self,
        provider: LLMProvider,
        collector: ResourceCollector,
        thresholds: ResourceThresholds,
    ) -> None:
        self._provider = provider
        self._collector = collector
        self._thresholds = thresholds
        self._engine = ThresholdEngine(thresholds)
        self._state = WatchdogState.RUNNING
        self._cpu_pressure_ticks = 0  # counts consecutive ticks above CPU threshold
        self._paused_at: float | None = None
        self._running = False
        self._last_reason: str = ""  # reason string from last state transition
        self._pause_trigger: str = ""  # "ram" | "cpu" | "gpu" | "game" | ""

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def state(self) -> WatchdogState:
        return self._state

    @property
    def last_reason(self) -> str:
        return self._last_reason

    async def run(self) -> None:
        """Main watchdog loop — runs until stop() is called."""
        self._running = True
        logger.info("watchdog started", extra={"state": self._state.value})

        interval = self._thresholds.check_interval_seconds
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                logger.error("watchdog tick error", extra={"error": str(exc)})
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        self._running = False
        logger.info("watchdog stopped")

    def notify_manual_pause(self) -> None:
        """
        Called by the API after a successful manual /pause.
        Syncs watchdog state so the auto-resume grace period starts from now.
        """
        self._state = WatchdogState.PAUSED
        self._paused_at = time.monotonic()
        self._last_reason = "manual pause"
        logger.info("watchdog state synced — manual pause")

    def notify_manual_resume(self) -> None:
        """
        Called by the API after a successful manual /resume.
        Bypasses evaluate_resume() — the model is already loaded, no room-check needed.
        """
        self._state = WatchdogState.RUNNING
        self._paused_at = None
        self._last_reason = "manual resume"
        self._pause_trigger = ""
        logger.info("watchdog state synced — manual resume")

    # ── Tick ──────────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        # Detect provider crashes so state doesn't stay stuck at RUNNING/PAUSED
        # when Ollama exits unexpectedly.  One lightweight health probe per tick.
        if self._state in (WatchdogState.RUNNING, WatchdogState.PAUSED):
            try:
                healthy = await self._provider.health_check()
            except Exception:
                healthy = False
            if not healthy:
                self._state = WatchdogState.PROVIDER_DOWN
                self._last_reason = "provider unreachable"
                logger.warning("provider unreachable — entering PROVIDER_DOWN")
                return
        elif self._state == WatchdogState.PROVIDER_DOWN:
            try:
                healthy = await self._provider.health_check()
            except Exception:
                healthy = False
            if not healthy:
                logger.debug("provider still down — waiting for recovery")
                return
            self._state = WatchdogState.RUNNING
            self._last_reason = "provider recovered"
            logger.info("provider recovered — returning to RUNNING")
            # Fall through: evaluate resources immediately on recovery tick

        metrics = self._collector.collect()
        game_detected, game_reason = _detect_game()
        resource_pressure, resource_reason = self._engine.evaluate(metrics)

        # Accumulate CPU ticks for sustained-seconds enforcement
        if resource_pressure and "CPU" in resource_reason:
            self._cpu_pressure_ticks += 1
        else:
            self._cpu_pressure_ticks = 0

        cpu_sustained = (
            self._cpu_pressure_ticks * self._thresholds.check_interval_seconds
            >= self._thresholds.cpu_sustained_seconds
        )

        # Resolve whether we should be paused right now
        should_pause = game_detected or (
            resource_pressure and ("CPU" not in resource_reason or cpu_sustained)
        )

        reason = game_reason or resource_reason

        # PAUSING and RESUMING are intra-tick transient states — they exist only
        # between the start of a transition method and when the provider call resolves.
        # _tick() awaits those transitions inline, so these states are never observed
        # at _tick() entry under normal operation; no branch is needed for them here.
        if self._state == WatchdogState.RUNNING and should_pause:
            # Record what triggered this pause for resume-gating logic
            if game_detected:
                self._pause_trigger = "game"
            elif "GPU" in resource_reason:
                # GPU checked before RAM — "GPU VRAM" contains the substring "RAM"
                self._pause_trigger = "gpu"
            elif "RAM" in resource_reason:
                self._pause_trigger = "ram"
            elif "CPU" in resource_reason:
                self._pause_trigger = "cpu"
            else:
                self._pause_trigger = "unknown"
            await self._transition_to_paused(reason)

        elif self._state == WatchdogState.PAUSED and not should_pause:
            # Grace period: don't resume immediately after pressure clears
            grace = self._thresholds.pause_timeout_seconds
            elapsed = time.monotonic() - (self._paused_at or 0)
            safe_to_resume, resume_reason = self._engine.evaluate_resume(metrics)

            # When auto_resume_on_ram_pressure is False, RAM-triggered pauses
            # require manual /resume — prevents model-eviction oscillation.
            if self._pause_trigger == "ram" and not self._thresholds.auto_resume_on_ram_pressure:
                logger.debug(
                    "auto-resume suppressed — RAM-triggered pause requires manual /resume",
                    extra={"auto_resume_on_ram_pressure": False},
                )
                return

            if safe_to_resume and elapsed >= grace:
                await self._transition_to_running(resume_reason)
            elif not safe_to_resume:
                logger.debug("resume deferred", extra={"reason": resume_reason})
            else:
                remaining = int(grace - elapsed)
                logger.debug("resume deferred — grace period", extra={"remaining_s": remaining})

    # ── Transitions ───────────────────────────────────────────────────────────

    async def _transition_to_paused(self, reason: str) -> None:
        self._state = WatchdogState.PAUSING
        logger.info("pausing", extra={"reason": reason})
        success = await self._provider.pause()
        if success:
            self._state = WatchdogState.PAUSED
            self._paused_at = time.monotonic()
            self._cpu_pressure_ticks = 0
            self._last_reason = reason
            logger.info("paused", extra={"reason": reason})
        else:
            self._state = WatchdogState.RUNNING
            logger.error("pause failed — remaining in RUNNING state", extra={"reason": reason})

    async def _transition_to_running(self, reason: str) -> None:
        self._state = WatchdogState.RESUMING
        logger.info("resuming", extra={"reason": reason})
        success = await self._provider.resume()
        if success:
            self._state = WatchdogState.RUNNING
            self._paused_at = None
            self._last_reason = reason
            logger.info("resumed", extra={"reason": reason})
        else:
            self._state = WatchdogState.PAUSED
            logger.error("resume failed — remaining in PAUSED state", extra={"reason": reason})


# ── Game detection ────────────────────────────────────────────────────────────


def _detect_game() -> tuple[bool, str]:
    """
    Scan running processes for Steam game executables.
    A process is considered a game if its exe path contains 'steamapps/common'
    (case-insensitive). Uses psutil — no shell, no subprocess.
    """
    try:
        for proc in psutil.process_iter(["exe"]):
            try:
                exe = (proc.info.get("exe") or "").replace("\\", "/").lower()
                if "steamapps/common" in exe:
                    # Use exe path component only — never log full path to avoid
                    # injecting arbitrary filesystem data into log strings
                    parts = exe.split("steamapps/common/")
                    game_dir = parts[1].split("/")[0] if len(parts) > 1 else "unknown"
                    return True, f"game detected — steamapps/common/{game_dir}"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as exc:
        logger.warning("game detection error", extra={"error": str(exc)})
    return False, ""
