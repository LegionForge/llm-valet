# Architecture

> **Applies to `v0.6.0`** — updated 2026-05-02

llm-valet is a cross-platform LLM lifecycle manager. It monitors machine resource pressure and gaming activity, then automatically pauses or resumes the LLM provider to free memory when the machine needs it and restore service when it does not.

---

## Component Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  llm-valet process                                              │
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐  │
│  │   api.py     │    │ watchdog.py  │    │  config.py       │  │
│  │  FastAPI app │    │  FSM + tick  │    │  Settings loader │  │
│  └──────┬───────┘    └──────┬───────┘    └──────────────────┘  │
│         │                  │                                    │
│         ▼                  ▼                                    │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              providers/                                  │  │
│  │  base.py — LLMProvider ABC + ProviderStatus              │  │
│  │  ollama.py — Ollama implementation (keep_alive API)      │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              resources/                                  │  │
│  │  base.py — ResourceCollector ABC + ThresholdEngine       │  │
│  │  macos.py — Apple Silicon (unified memory + Metal GPU)   │  │
│  │  linux.py — psutil + pynvml / ROCm                       │  │
│  │  windows.py — psutil + WMI + pynvml                      │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              svcmgr/                                     │  │
│  │  macos.py — launchctl + osascript (Brew + App variants)  │  │
│  │  linux.py — systemd --user + direct spawn fallback       │  │
│  │  windows.py — sc.exe + direct launch fallback            │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  static/index.html — single-file WebUI                         │
└─────────────────────────────────────────────────────────────────┘
```

| Component | Role |
|-----------|------|
| `llm_valet/api.py` | FastAPI application — HTTP control surface, security middleware, WebUI serving |
| `llm_valet/watchdog.py` | Autonomous FSM — combines resource signals and game detection into pause/resume decisions |
| `llm_valet/config.py` | Settings loader — reads `~/.llm-valet/config.yaml`, applies env var overrides |
| `llm_valet/providers/base.py` | `LLMProvider` ABC — defines the interface all provider implementations satisfy |
| `llm_valet/providers/ollama.py` | Ollama-specific implementation — model eviction via `keep_alive` API |
| `llm_valet/resources/base.py` | `ResourceCollector` ABC and `ThresholdEngine` — pure metric types and threshold logic |
| `llm_valet/resources/macos.py` | macOS collector — Apple Silicon unified memory pressure + Metal GPU |
| `llm_valet/resources/linux.py` | Linux collector — psutil + pynvml / ROCm |
| `llm_valet/resources/windows.py` | Windows collector — psutil + WMI + pynvml |
| `svcmgr/macos.py` | macOS service manager — handles both Ollama.app and Homebrew CLI installs |
| `svcmgr/linux.py` | Linux service manager — systemd user unit with direct-spawn fallback |
| `svcmgr/windows.py` | Windows service manager — sc.exe with direct-launch fallback |
| `static/index.html` | Single-file WebUI — no framework, no build step |

---

## Two Parallel Abstractions

The architecture has two symmetric abstraction hierarchies, one for each concern:

```
providers/              resources/
  base.py (ABC)           base.py (ABC)
  ollama.py               macos.py
  # post-v1.0:            linux.py
  # lmstudio.py           windows.py
  # vllm.py
  # mlx.py
```

**Why symmetric?** `api.py` and `watchdog.py` both need both halves. By making each half an abstract interface, the concrete implementations can be swapped independently — a new provider (LM Studio, vLLM) or a new platform (Linux, Windows) can be added without touching the other hierarchy or the application logic.

**Dependency injection, not imports.** `api.py` never imports a concrete provider or collector directly. Both are constructed in `_build_provider()` and `_build_collector()` based on `settings.provider` and `sys.platform`, then injected as FastAPI dependencies. This keeps the application layer platform-neutral.

**Pause vs. stop is a first-class distinction** — not an implementation detail. Two levels of unloading exist and must never be conflated:

| Action | Effect | Speed | When used |
|--------|--------|-------|-----------|
| **Pause / Resume** | Unloads model from memory; provider service stays running | Fast (seconds) | Default: resource pressure, gaming detected |
| **Stop / Start** | Full service shutdown via platform service manager | Slow (30–90s) | Manual maintenance or zero-memory-footprint requirement |

The `LLMProvider` ABC exposes both pairs separately (`pause`/`resume` and `stop`/`start`) so callers cannot accidentally conflate them.

---

## Watchdog FSM

`llm_valet/watchdog.py` implements a five-state machine driven by a polling loop. The loop runs at `check_interval_seconds` (default 10s). All state transitions are logged with a structured reason string.

### States

| State | Meaning |
|-------|---------|
| `RUNNING` | Provider is up; model is loaded and available |
| `PAUSING` | Transitioning to paused — `provider.pause()` in flight |
| `PAUSED` | Model evicted from memory; provider service still running |
| `RESUMING` | Transitioning to running — `provider.resume()` in flight |
| `PROVIDER_DOWN` | Provider process not reachable — waiting for recovery |

`PAUSING` and `RESUMING` are intra-tick transient states. They exist only for the duration of the async provider call. Under normal operation they are never observed at tick entry.

### Transition Conditions

```
RUNNING  → PAUSING       game detected OR (resource pressure AND sustained)
PAUSING  → PAUSED        provider.pause() returned True
PAUSING  → RUNNING       provider.pause() returned False (stay up, log error)
PAUSED   → RESUMING      no pressure AND grace period elapsed AND safe_to_resume
                         (unless RAM-triggered and auto_resume_on_ram_pressure=False)
RESUMING → RUNNING       provider.resume() returned True
RESUMING → PAUSED        provider.resume() returned False (stay paused, log error)
RUNNING  → PROVIDER_DOWN health_check() returns False
PAUSED   → PROVIDER_DOWN health_check() returns False
PROVIDER_DOWN → RUNNING  health_check() returns True again (recovery tick)
```

### Tick Loop Logic

Each tick executes `_tick()`, which follows this sequence:

1. **Health probe** — call `provider.health_check()`. If False while in `RUNNING` or `PAUSED`, transition to `PROVIDER_DOWN` and return. If in `PROVIDER_DOWN` and now True, transition to `RUNNING` and continue.

2. **Collect metrics** — `collector.collect()` → `SystemMetrics`.

3. **Game detection** — scan running processes via psutil for exe paths containing `steamapps/common` (case-insensitive). No shell, no subprocess.

4. **Threshold evaluation** — `ThresholdEngine.evaluate(metrics)` → `(resource_pressure, resource_reason)`.

5. **CPU sustained-seconds** — accumulate `_cpu_pressure_ticks` while CPU threshold is breached; clear the counter when CPU drops below threshold. The pause only triggers when `ticks * check_interval_seconds >= cpu_sustained_seconds`.

6. **Pause decision** — `should_pause = game_detected OR (resource_pressure AND ("CPU" not in reason OR cpu_sustained))`. RAM and GPU trigger immediately; CPU requires the sustained window.

7. **RUNNING and should_pause** — record the `_pause_trigger` (`"ram"`, `"cpu"`, `"gpu"`, or `"game"`), then call `_transition_to_paused(reason)`.

8. **PAUSED and not should_pause** — check the grace period (`pause_timeout_seconds`, default 120s since pause). Call `evaluate_resume(metrics)`. If `_pause_trigger == "ram"` and `auto_resume_on_ram_pressure is False`, skip auto-resume and require manual `/resume`. Otherwise, if `safe_to_resume` and grace elapsed, call `_transition_to_running(resume_reason)`.

**Manual sync.** When the API handles a `/pause` or `/resume` request, it calls `provider.pause()` or `provider.resume()` directly, then calls `watchdog.notify_manual_pause()` or `watchdog.notify_manual_resume()`. These methods sync the watchdog's internal state so the grace period and trigger tracking stay consistent with reality.

---

## ThresholdEngine

`ThresholdEngine` lives in `llm_valet/resources/base.py`. It is pure logic — stateless beyond the threshold configuration, no I/O. This makes it straightforward to unit-test without mocking any system calls.

### Interface

```python
class ThresholdEngine:
    def __init__(self, thresholds: ResourceThresholds) -> None: ...

    def evaluate(self, metrics: SystemMetrics) -> tuple[bool, str]:
        # Returns (should_pause, reason).
        # Caller tracks CPU sustained-seconds externally.
        # RAM and GPU trigger immediately; CPU signals True whenever the
        # threshold is exceeded (caller counts ticks).

    def evaluate_resume(self, metrics: SystemMetrics) -> tuple[bool, str]:
        # Returns (safe_to_resume, reason).
        # All metrics must be below resume thresholds.
```

### Hysteresis

RAM uses two separate thresholds to prevent oscillation:

- **Pause at** `ram_pause_pct` (default 85%) — triggers model eviction.
- **Resume at** `ram_resume_pct` (default 60%) — much lower than the pause threshold.

Without this gap, a machine where the model itself is the dominant RAM consumer would pause, see RAM fall slightly after eviction, resume, reload the model, immediately breach the threshold again, and repeat. The 25% default gap provides meaningful breathing room. The config validator enforces `ram_resume_pct < ram_pause_pct` and rejects any update that violates this invariant.

CPU and GPU resume at their respective pause thresholds, since the watchdog's `pause_timeout_seconds` grace period provides a sustained buffer on the resume side.

### CPU Sustained-Seconds

CPU pressure must be sustained for `cpu_sustained_seconds` (default 30s) before triggering a pause. A transient CPU spike — a compiler run, an indexer flush — should not cause a model eviction. The watchdog accumulates `_cpu_pressure_ticks` in the outer loop and checks `ticks * interval >= cpu_sustained_seconds`. The counter resets to zero the moment CPU drops below the threshold.

### PressureLevel — Informational Only

`PressureLevel` (NORMAL / WARN / CRITICAL) is collected and reported in `/metrics` but intentionally not used as a pause trigger. On Apple Silicon, loading a large model into unified memory routinely produces transient CRITICAL pressure readings even when RAM% is within the user's configured threshold. Using CRITICAL as an override would defeat the purpose of `ram_pause_pct` configuration.

---

## Security Model

The security model is documented in full in `SECURITY.md`. The threats relevant to the architecture are summarized here.

| ID | Threat | Mitigation |
|----|--------|------------|
| T1 | Unauthenticated service control | `127.0.0.1` bind by default; `X-API-Key` required for LAN access |
| T2 | DNS rebinding | `TrustedHostMiddleware` — allowlist `localhost`, `127.0.0.1`, `*.local` |
| T3 | CORS wildcard | `allow_origins` is config-only and empty by default; never `"*"` |
| T4 | Command injection | `subprocess.run(list, shell=False)` always; model names validated against `^[a-zA-Z0-9:._-]{1,200}$` |
| T5 | WebUI XSS | `element.textContent` everywhere for dynamic data; never `innerHTML` |
| T6 | SSRF via provider URL | `ollama_url` validated — scheme must be `http`/`https`, host must be localhost or RFC1918 |
| T7 | Privilege escalation | User-level services only; `api.py` exits immediately if `os.getuid() == 0` |
| T8 | Config file permissions | `config.yaml` written with `chmod 0600`; startup warns if world-readable |

The `require_api_key` dependency in `api.py` uses `hmac.compare_digest` for constant-time comparison. Auth is skipped only when the client address is `127.0.0.1` or `::1`.

---

## Language and Runtime

llm-valet is written in Python 3.11+. This was a deliberate choice, not a default.

### Why Python

**psutil is the deciding factor.** psutil is a cross-platform library that provides process enumeration, RAM usage, CPU load, and GPU metrics through a single unified API on macOS, Linux, and Windows. It handles the platform differences internally — the same Python call works whether the machine is an Apple Silicon Mac, an AMD Linux box, or a Windows workstation. No equivalent exists in other languages at the same level of maturity and breadth.

Without psutil, the resource monitoring layer would require native platform bindings or shell-outs to OS tools on each platform — which is exactly what psutil already solves, and has solved for over a decade.

**pip distribution fits the target user.** Homebrew installs Python as part of Ollama's dependency chain on macOS. The likely llm-valet user already has Python. `pip install legionforge-llm-valet` requires no additional runtime, no per-platform binary compilation, no package manager beyond what's already present.

**FastAPI + asyncio fit the architecture.** The watchdog is an async polling loop; the REST API is async; the Ollama HTTP client is async. FastAPI provides all of this plus automatic OpenAPI documentation with minimal boilerplate. The performance requirements are trivial — a daemon that wakes every 10 seconds and makes one HTTP call has no meaningful runtime overhead in any language.

### Why not other languages

**Node.js / TypeScript** is a reasonable alternative and would have worked. Node is genuinely cross-platform, TypeScript provides strong typing, and the HTTP client story is good. The gap is resource monitoring: there is no Node equivalent of psutil. Cross-platform process enumeration and RAM/GPU metrics would require native bindings or OS-specific shell commands on each platform.

**Rust or Go** would produce a leaner binary and faster startup, but neither advantage matters for a sleeping daemon. Both would require pre-compiled binaries per platform rather than a pip install, and both have less mature equivalents for psutil's breadth of coverage.

**The performance argument doesn't apply here.** llm-valet is not doing computation — it's polling a REST API and sleeping. A Python process in this role uses roughly 15 MB of RAM and negligible CPU. If a future version needed to process high-frequency metrics streams or handle thousands of concurrent connections, the language choice would be worth revisiting. For a 10-second polling daemon, it doesn't matter.

---

## Key Design Decisions

### Provider abstraction

`api.py` depends on `LLMProvider` (ABC), not on `OllamaProvider`. The concrete instance is injected at startup. Post-v1.0 providers (LM Studio, vLLM, MLX) will be added as new files in `providers/` without touching `api.py` or `watchdog.py`.

### Pause vs. stop distinction

Pause (model eviction via `keep_alive=0`) is the default action for all automatic events. Stop (full service shutdown) is reserved for explicit manual requests. This distinction prevents the 30–90s Ollama startup time from affecting the auto-resume path — the service is always running; only the model's memory residency changes.

### User-level services only

`svcmgr/` manages user-level service agents exclusively — launchd `gui/<uid>` agents on macOS, `systemd --user` on Linux. Root is never required. If only a root-owned system service exists (common on Linux), `stop_service()` returns False and `ollama.py` falls back to psutil SIGTERM.

### Default bind: 127.0.0.1

The service binds `127.0.0.1` unless the user explicitly sets `host: 0.0.0.0` in config. LAN exposure is opt-in and requires `api_key` to be set — the API rejects all non-localhost requests if `api_key` is empty and the host is `0.0.0.0`.

### Overcommit warning

At startup and on every `/status` request, `api.py` compares the loaded model's memory footprint (from Ollama `/api/ps`) against the RAM pause threshold. If the model alone exceeds the threshold, the watchdog's auto-pause will never fire — the threshold is already breached before additional load arrives. A structured warning is logged and the `/status` response includes an `overcommit: true` flag.
