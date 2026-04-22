# llm-valet Watchdog — State Machine Reference

Every N seconds (default: 10 s) the watchdog asks three questions:

1. **Is a Steam game running?** (scans process list for `steamapps/common` paths)
2. **Are RAM / CPU / GPU above the configured thresholds?**
3. **Is Ollama still alive?** (lightweight health probe)

Then it acts — loading or unloading the model, or doing nothing if everything is fine.

---

## The Five States

| State | What it means | Stable? |
|-------|---------------|---------|
| **RUNNING** | Model is loaded and serving requests | ✅ Stable |
| **PAUSED** | Model is unloaded; RAM and GPU memory are free | ✅ Stable |
| **PROVIDER_DOWN** | Ollama is not responding (crashed or unreachable) | ✅ Stable |
| **PAUSING** | In the middle of unloading the model | ⚡ Transient (one tick) |
| **RESUMING** | In the middle of reloading the model | ⚡ Transient (one tick) |

**Transient** means the state lasts only as long as the Ollama API call takes — typically milliseconds. From the outside, you will rarely see PAUSING or RESUMING in `/status` unless the provider is very slow.

---

## State Diagram

```
                        ┌──────────────────────────┐
         ┌──────────────│         RUNNING           │◄──────────────────────────┐
         │              │                           │                            │
         │  [1] game    │  model is loaded,         │  [5] Ollama comes back     │
         │  detected    │  requests are served      │      online                │
         │  OR RAM/CPU  │                           │                            │
         │  /GPU spike  │  ◄── /resume (manual) ───│                            │
         │              └──────────────┬────────────┘                            │
         │                             │ [4] health check fails                  │
         │                             ▼                                         │
         │              ┌──────────────────────────┐                            │
         │              │      PROVIDER_DOWN        │────────────────────────────┘
         │              │                           │
         │              │  Ollama crashed or is     │
         │              │  not responding           │
         │              └──────────────────────────┘
         │
         ▼
 ┌───────────────────┐
 │     PAUSING       │──── [1b] pause call fails ──────────────► back to RUNNING
 │    (transient)    │
 └────────┬──────────┘
          │ [1b] pause call succeeds
          ▼
 ╔═══════════════════════════════════════════════════════════════╗
 ║                         PAUSED                                ║
 ║                                                               ║
 ║  Model is unloaded. RAM and GPU memory are free.             ║
 ║                                                               ║
 ║  ◄── /pause (manual override sets state directly)           ║
 ║                                                               ║
 ║  STAYS PAUSED while any of these are true:                  ║
 ║                                                               ║
 ║   [A]  game is still running                                 ║
 ║   [B]  RAM / CPU / GPU still above the pause threshold       ║
 ║   [C]  grace period has not fully elapsed                    ║
 ║   [D]  RAM triggered the pause AND                           ║
 ║        auto_resume_on_ram_pressure = false                   ║
 ║   [E]  RAM is in the hysteresis zone —                       ║
 ║        above ram_resume_pct but below ram_pause_pct          ║
 ║                                                               ║
 ║  [4]  Ollama crashes here → moves to PROVIDER_DOWN           ║
 ╚═══════════════════════════════╤═══════════════════════════════╝
                                  │ [2] all conditions [A–E] gone
                                  │     + grace period elapsed
                                  │     + safe to resume
                                  ▼
                        ┌──────────────────────┐
                        │      RESUMING         │──── [2b] resume fails ────► back to PAUSED
                        │     (transient)       │
                        └──────────┬────────────┘
                                   │ [2b] resume call succeeds
                                   └────────────────────────────► back to RUNNING
```

---

## Transitions in Plain English

| # | From | To | What causes it |
|---|------|----|----------------|
| 1 | RUNNING | PAUSING | A Steam game opened, OR RAM/CPU/GPU crossed the pause threshold |
| 1b | PAUSING | PAUSED | Ollama accepted the unload call (keep_alive: 0) |
| 1b | PAUSING | RUNNING | Ollama rejected or timed out — stays running, logs an error |
| 2 | PAUSED | RESUMING | All pressure gone + grace period elapsed + RAM below resume threshold |
| 2b | RESUMING | RUNNING | Ollama accepted the reload call (keep_alive: -1) |
| 2b | RESUMING | PAUSED | Ollama rejected or timed out — stays paused, logs an error |
| 3 | — | PAUSED | `/pause` API call — sets state directly, no PAUSING transient |
| 3 | — | RUNNING | `/resume` API call — sets state directly, no RESUMING transient |
| 4 | RUNNING | PROVIDER_DOWN | Health check failed (Ollama not responding) |
| 4 | PAUSED | PROVIDER_DOWN | Health check failed while paused |
| 5 | PROVIDER_DOWN | RUNNING | Health check passed — Ollama is back |
| 5+ | PROVIDER_DOWN | PAUSED | Recovered, but resources are immediately above threshold |

---

## Why Hysteresis Exists (the "safe zone")

If the watchdog paused because RAM hit 88% and then auto-resumed the moment RAM dropped to 84%, it would immediately trigger again — the model loading *is* the thing pushing RAM to 88%. This infinite loop is called **oscillation**.

The fix is a gap between two thresholds:

```
  0%          ram_resume_pct        ram_pause_pct        100%
  │───────────────────│─────────────────────│────────────│
       safe to resume      HYSTERESIS ZONE      must pause
                           (stay paused here)
```

Default: `ram_resume_pct = 60%`, `ram_pause_pct = 85%`. The model must consume less than 60% RAM before an auto-resume is allowed.

---

## Why PROVIDER_DOWN Is Its Own State

Before this state existed, if Ollama crashed unexpectedly, `/status` still showed `state: running` — technically the watchdog was running, but Ollama wasn't. This was confusing and masked real failures.

PROVIDER_DOWN makes the crash visible in the UI and API. The watchdog keeps polling for recovery and transitions back to RUNNING automatically when Ollama comes back online — no manual restart needed.

---

## Manual Override Behaviour

`/pause` and `/resume` set state **directly** — they bypass PAUSING and RESUMING entirely. This means:

- `/pause` → immediately `state: paused`, grace period timer starts now
- `/resume` → immediately `state: running`, `pause_trigger` cleared

This is intentional: manual overrides are explicit user intent and should take effect immediately, not queue behind a provider API call.

The API endpoints call `watchdog.notify_manual_pause()` / `watchdog.notify_manual_resume()` after the provider call succeeds. If the watchdog is not notified, its state will diverge from the provider's state — the next auto tick will then act on stale information.

---

## Configuration Knobs That Affect State Transitions

| Setting | Default | What it controls |
|---------|---------|-----------------|
| `ram_pause_pct` | 85.0 | RAM % that triggers a pause |
| `ram_resume_pct` | 60.0 | RAM % required before auto-resume |
| `cpu_pause_pct` | 90.0 | CPU % that begins the sustained-seconds count |
| `cpu_sustained_seconds` | 30 | How long CPU must be above threshold before pausing |
| `gpu_vram_pause_pct` | 85.0 | GPU VRAM % that triggers a pause |
| `pause_timeout_seconds` | 120 | Grace period — how long to stay paused after pressure clears |
| `auto_resume_on_ram_pressure` | true | If false, RAM-triggered pauses require manual /resume |
| `check_interval_seconds` | 10 | How often the watchdog runs a tick |
