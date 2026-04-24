# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Testing — Live Doc Updates (Non-Negotiable)

When executing or reviewing Mac Mini tests, update `docs/testing/<version>-mac-mini.md` immediately when each test passes or fails — do not batch updates until the end of a session:

- Mark each check row `✅` (pass) or `❌` (fail) in the Pass? column, with a Notes entry for anything non-obvious
- Update the Results summary table at the bottom of the file after each test group completes
- If a bug is found during testing, note the commit that fixed it in the relevant check row
- Change the Results header from "TBD" to "In Progress" once testing begins, and to "COMPLETE" with overall outcome once all tests are done

This applies whether tests are run interactively via SSH or by reviewing session notes.

---

## GitHub Pre-commit Scrub Rules

Before committing or creating a PR, verify these are NOT present in any tracked file:

- **IPs / hostnames** — dev/test machine IPs (e.g. LAN RFC1918 addresses), internal hostnames, mDNS names (`.local`) of test hardware
- **Usernames** — SSH usernames, system account names, OS usernames of test participants other than the public project author (JP Cruz / jp@legionforge.org)
- **SSH details** — key paths, key fingerprints, `user@host` patterns, port numbers of internal machines
- **Network topology** — which machine is on which IP, subnet layout, router info
- **API keys / tokens / passwords** — even test/temporary ones

**What is allowed:** `localhost`, `127.0.0.1`, `0.0.0.0`, `<user>`, `<hostname>`, `<ip>` as placeholders. Generic port numbers that are part of the documented interface (e.g. 8765, 11434) are fine.

**When writing test docs or session notes:** Use placeholders from the start. Never copy-paste raw `ssh user@ip` commands or `ls -la` output containing system usernames into committed files.

---

## Project Overview

**llm-valet** (`LegionForge/llm-valet`) is a cross-platform drop-in utility that manages Ollama (and other LLM providers) lifecycle based on manual control or automatic resource/activity sensing. Target platforms: macOS, Windows, Linux.

**Origin use case:** A Mac Mini M4 doubles as both a persistent LLM server and a gaming machine. The valet detects when gaming is happening (or resources are scarce) and gracefully unloads the model and optionally the LLM service, then reloads when resources free up.

Published as a free open-source utility under the LegionForge GitHub organization.

### Market Gap (Why This Exists)

A thorough search of existing tools (April 2026) confirmed this fills a real gap. No existing project combines:
- Automatic pause/resume based on real-time resource pressure thresholds
- Gaming activity detection (Steam native process watching)
- Cross-platform REST API + web dashboard with manual override
- Provider abstraction (Ollama for v1.0; additional providers post-v1.0)

**Nearest neighbors and why they don't overlap:**
- **Open WebUI** (130k+ stars): chat UI only — no lifecycle control, no resource management
- **EnviroLLM**: energy/resource benchmarking and advisory — monitoring only, not automatic control
- **OllamaMan / ollama-dashboard**: read-only dashboards — no pause/resume, no thresholds
- **Ollama built-in `keep_alive`**: time-based idle unload only — no resource pressure sensing, no gaming detection, no cross-provider support

The GitHub issue ollama/ollama#11085 documents community demand for resource-pressure-based unloading that Ollama has not implemented.

## Architecture

### Core Distinction: Pause vs. Stop

Two levels of unloading exist and must be kept separate:

- **Pause/Resume** — Unloads the model from memory but leaves the provider service running. Fast: reloads in seconds. Default action for resource-pressure events.
- **Stop/Start** — Full service shutdown via platform service manager. Slow: 30–90s to come back. Reserved for maintenance or zero-memory-footprint needs.

### Two Parallel Abstractions

The architecture has two symmetric abstraction hierarchies:

```
providers/          ← what serves the LLM
  base.py           LLMProvider ABC
  ollama.py         Ollama (v1.0)
                    # post-v1.0: LM Studio, vLLM, MLX (Apple Silicon)

resources/          ← what monitors the machine
  base.py           ResourceCollector ABC + ThresholdEngine
  macos.py          Apple Silicon: unified memory pressure + Metal GPU
  linux.py          psutil + pynvml / ROCm
  windows.py        psutil + WMI + pynvml
```

`watchdog.py` consumes one of each and combines them into automated decisions. `api.py` consumes both for the control surface and `/metrics` endpoint.

### Full Component Layout

```
llm-valet/
├── llm_valet/
│   ├── api.py              # FastAPI app — HTTP endpoints + security middleware
│   ├── watchdog.py         # Auto-mode: process watcher + resource signal consumer
│   ├── config.py           # Settings loader (config.yaml or env vars)
│   ├── providers/
│   │   ├── base.py         # LLMProvider ABC + ProviderStatus dataclass
│   │   └── ollama.py       # Ollama implementation (post-v1.0: LM Studio, vLLM, MLX)
│   └── resources/
│       ├── base.py         # ResourceCollector ABC, metric dataclasses, ThresholdEngine
│       ├── macos.py        # macOS implementation
│       ├── linux.py        # Linux implementation
│       └── windows.py      # Windows implementation
├── svcmgr/
│   ├── macos.py            # launchctl user agent management
│   ├── linux.py            # systemd --user management
│   └── windows.py          # Windows Service / sc.exe
├── static/
│   └── index.html          # Single-file WebUI
├── install/
│   ├── install.sh
│   └── install.ps1
├── pyproject.toml
└── README.md
```

### Resource Abstraction (`resources/base.py`)

```python
import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

class PressureLevel(enum.Enum):
    NORMAL = "normal"
    WARN = "warn"
    CRITICAL = "critical"

@dataclass
class MemoryMetrics:
    total_mb: int
    used_mb: int
    used_pct: float
    pressure: PressureLevel     # from OS pressure API where available

@dataclass
class CPUMetrics:
    used_pct: float             # 1-second average
    core_count: int

@dataclass
class GPUMetrics:
    available: bool             # False if no GPU driver accessible
    vram_total_mb: Optional[int]
    vram_used_mb: Optional[int]
    vram_used_pct: Optional[float]
    compute_pct: Optional[float]

@dataclass
class SystemMetrics:
    memory: MemoryMetrics
    cpu: CPUMetrics
    gpu: GPUMetrics
    timestamp: float

class ResourceCollector(ABC):
    @abstractmethod
    def collect(self) -> SystemMetrics: ...

    @abstractmethod
    def supported_metrics(self) -> set[str]: ...
    # e.g. {"memory", "cpu", "gpu", "pressure"}
    # Callers check this before trusting Optional fields
```

**Platform-specific metric sources:**

| Metric | macOS | Linux | Windows |
|--------|-------|-------|---------|
| RAM % | `psutil` | `psutil` | `psutil` |
| Memory pressure level | `memory_pressure` CLI (normal/warn/critical) | `psutil` thresholds | `psutil` thresholds |
| CPU % | `psutil` | `psutil` | `psutil` |
| GPU VRAM | `system_profiler` / `ioreg` (Metal) | `pynvml` / `/sys/class/drm` (AMD) | `pynvml` / WMI |
| GPU compute | `ioreg` | `pynvml` | WMI |

On macOS M-series, **unified memory pressure level** from the `memory_pressure` CLI is more meaningful than raw RAM % because GPU and CPU share the same pool. Prefer this when available.

**`ThresholdEngine`** lives in `resources/base.py`. Takes `SystemMetrics` + `ResourceThresholds`, returns `(should_pause: bool, reason: str)`. Pure logic, no I/O — straightforward to unit test.

```python
@dataclass
class ResourceThresholds:
    ram_pause_pct: float = 85.0
    ram_resume_pct: float = 60.0     # hysteresis gap prevents oscillation
    cpu_pause_pct: float = 90.0
    cpu_sustained_seconds: int = 30  # must exceed threshold for this long before acting
    gpu_vram_pause_pct: float = 85.0
    pause_timeout_seconds: int = 120 # grace period before resume after resource clears
    check_interval_seconds: int = 10

class ThresholdEngine:
    def __init__(self, thresholds: ResourceThresholds): ...
    def evaluate(self, metrics: SystemMetrics) -> tuple[bool, str]:
        """Stateless — caller tracks sustained-seconds externally."""
        ...
```

### Provider Interface (`providers/base.py`)

```python
@dataclass
class ProviderStatus:
    running: bool
    model_loaded: bool
    model_name: Optional[str]
    memory_used_mb: Optional[int]

class LLMProvider(ABC):
    @abstractmethod
    async def start(self) -> bool: ...
    @abstractmethod
    async def stop(self) -> bool: ...
    @abstractmethod
    async def pause(self) -> bool: ...
    @abstractmethod
    async def resume(self) -> bool: ...
    @abstractmethod
    async def status(self) -> ProviderStatus: ...
    @abstractmethod
    async def health_check(self) -> bool: ...
```

The active provider is selected from config (`provider: ollama`) and injected as a FastAPI dependency. `api.py` never imports a concrete provider directly.

### Watchdog (`watchdog.py`)

Combines game-process detection with resource collector signals. Holds references to a `LLMProvider` and a `ResourceCollector` — never calls psutil or any platform API directly.

```python
class Watchdog:
    def __init__(
        self,
        provider: LLMProvider,
        collector: ResourceCollector,
        thresholds: ResourceThresholds,
    ): ...

    async def run(self) -> None:
        # Every check_interval_seconds:
        # 1. collector.collect() → SystemMetrics
        # 2. Check psutil for processes whose exe path contains steamapps/common
        # 3. ThresholdEngine.evaluate(metrics) → (resource_pressure, reason)
        # 4. pause if: game detected OR resource_pressure
        # 5. resume only if: no game AND NOT resource_pressure AND grace period elapsed
        # 6. Execute provider.pause() / provider.resume() on state change
        # 7. Log structured entry with reason on every state transition
```

State machine: `RUNNING → PAUSING → PAUSED → RESUMING → RUNNING`. Every transition records its trigger reason (e.g., `"paused — RAM 87% > 85% threshold"` or `"paused — steamapps/common/Hades detected"`).

### API Endpoints

| Method | Path | Action |
|--------|------|--------|
| GET | `/status` | Provider state + current resource snapshot |
| GET | `/metrics` | Live `SystemMetrics` from `ResourceCollector` |
| POST | `/pause` | Manual pause |
| POST | `/resume` | Manual resume |
| POST | `/start` | Full service start |
| POST | `/stop` | Graceful service shutdown |
| POST | `/restart` | stop → sleep(2) → start |
| GET | `/config` | Read current thresholds + watchdog settings |
| PUT | `/config` | Update thresholds at runtime (persisted to config.yaml) |
| GET | `/docs` | Auto-generated OpenAPI docs |

### WebUI (`static/index.html`)

Single file, no framework, no build step. Dark monospace theme.

- Resource bars: RAM, CPU, GPU (if `gpu.available`) with pause threshold markers
- State badge: `RUNNING` / `PAUSED` / `STOPPED`
- Last action log: reason string from last watchdog state transition
- Threshold sliders: PUT to `/config` on change
- Manual buttons: PAUSE / RESUME / STOP / START
- All dynamic values via `element.textContent` — never `innerHTML`

## Stack

- **Python 3.11+** with **FastAPI** and **uvicorn**
- **httpx** for async provider API calls
- **psutil** for cross-platform process enumeration, RAM, CPU
- **pynvml** (optional) for NVIDIA GPU on Linux/Windows
- **PyYAML** for config
- WebUI: single `static/index.html`

## Security Model

Security must be built in from the start. This tool executes system commands and listens on a network port. CVE-2025-66416 (DNS rebinding in MCP Python SDK) demonstrates the exact attack pattern that applies here.

### Threat Model

**T1 — Unauthenticated Service Control**
Anyone reaching port 8765 can pause/stop the LLM service.
- Mitigation: Bind `127.0.0.1` by default. `X-API-Key` header required when `host: 0.0.0.0` set in config.

**T2 — DNS Rebinding**
Malicious site rebinds DNS to LAN IP; browser JS controls the local service.
- Mitigation: `TrustedHostMiddleware` — allowlist `localhost`, `127.0.0.1`, `*.local`. All other Host headers → 400.

**T3 — CORS Wildcard**
Cross-origin JS hits the API.
- Mitigation: Never `allow_origins=["*"]`. Default same-origin. LAN origins require explicit config entry.

**T4 — Command Injection**
Model names interpolated into subprocess calls.
- Mitigation: `subprocess.run(["ollama", "stop", model_name], shell=False, ...)` always. Model names validated against `^[a-zA-Z0-9:._-]+$`.

**T5 — WebUI XSS**
API-sourced data rendered via `innerHTML` executes JS.
- Mitigation: `element.textContent` everywhere for dynamic data.

**T6 — SSRF via Provider URL**
Configurable provider URL redirected to internal services.
- Mitigation: Validate scheme (`http`/`https`) + host (localhost / RFC1918 only).

**T7 — Privilege Escalation**
Managing root-owned system services.
- Mitigation: User-level services only. Never run as root.

**T8 — Config File Permissions**
API key in world-readable config.
- Mitigation: `chmod 0600 ~/.llm-valet/config.yaml` on write. Warn on startup if too permissive.

### Required Security Defaults in `api.py`

```python
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1", "*.local"]  # extended via config
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],        # from config only — never "*"
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["X-API-Key"],
)

async def require_api_key(request: Request, x_api_key: str = Header(default="")):
    if request.client.host != "127.0.0.1" and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
```

### subprocess Safety Rule

```python
# CORRECT
subprocess.run(["ollama", "stop", model_name], capture_output=True, text=True, timeout=30)

# BANNED
subprocess.run(f"ollama stop {model_name}", shell=True)
```

## Key Implementation Notes

### Ollama Variant Detection (Do This First)

macOS has two Ollama install paths with different plist locations:
- **App**: `/Applications/Ollama.app` — check this first
- **Brew CLI**: `~/Library/LaunchAgents/com.ollama.ollama.plist`

`svcmgr/macos.py` must branch on which variant is present.

### Graceful Stop Sequence

```
1. provider.pause()             # unload model cleanly first
2. SIGTERM to provider process
3. Poll health_check() until False, timeout=30s
4. SIGKILL if still alive
5. platform.stop_service()      # prevent respawn
```

### Logging

Structured JSON logging → rotating file at `~/.llm-valet/valet.log`. No `print()`. Never interpolate raw user input or API response data into log strings — use `extra={}` kwargs to prevent log injection.

### ResourceCollector Selection

`config.py` selects the collector at startup:
```python
if sys.platform == "darwin":
    from llm_valet.resources.macos import MacOSResourceCollector as Collector
elif sys.platform == "linux":
    from llm_valet.resources.linux import LinuxResourceCollector as Collector
else:
    from llm_valet.resources.windows import WindowsResourceCollector as Collector
```

The selected instance is injected into both `Watchdog` and registered as a FastAPI dependency for `/metrics`.

## Development Setup

```bash
pip install fastapi uvicorn httpx psutil pyyaml
pip install pynvml  # optional — NVIDIA GPU only

# Run (localhost only — default)
uvicorn llm_valet.api:app --host 127.0.0.1 --port 8765 --reload

# Run with LAN exposure (api_key must be set in config first)
uvicorn llm_valet.api:app --host 0.0.0.0 --port 8765

open http://localhost:8765        # WebUI
open http://localhost:8765/docs   # API docs
```

## Manual Control

```bash
# Localhost (no auth)
curl http://localhost:8765/status
curl http://localhost:8765/metrics
curl -X POST http://localhost:8765/pause
curl -X POST http://localhost:8765/resume

# LAN (X-API-Key required)
curl -H "X-API-Key: your-key" -X POST http://mac-mini.local:8765/pause
```

## Build Order

1. `resources/base.py` — dataclasses, `ResourceCollector` ABC, `ThresholdEngine` (pure logic, unit-testable first)
2. `providers/base.py` — `LLMProvider` ABC, `ProviderStatus`
3. `resources/macos.py` — concrete collector; verify `collect()` returns valid metrics on M-series
4. `providers/ollama.py` — pause, resume, status, health_check; test against real Ollama
5. `api.py` — FastAPI with all security middleware; validate every endpoint with curl
6. `static/index.html` — resource bars, state badge, threshold sliders; `textContent` only
7. `watchdog.py` — process detection + resource collector integration; test state transitions
8. `svcmgr/macos.py` — launchctl; handle both Ollama install variants
9. `resources/linux.py` + `resources/windows.py` — remaining platforms
10. `install/install.sh` + `install.ps1` — last, after app is stable

## Decided Architecture Choices

- **Repo:** `LegionForge/llm-valet`
- **Two abstraction layers:** `providers/` (LLM serving) and `resources/` (machine monitoring) — symmetric design
- **Watchdog is resource-aware:** receives `ResourceCollector` instance; never calls psutil directly
- **ThresholdEngine is pure logic:** stateless, no I/O — accepts metrics + thresholds, returns decision
- **Hysteresis:** pause at `ram_pause_pct`, resume only below `ram_resume_pct`
- **CPU sustained-seconds:** must exceed threshold for N seconds before triggering
- **macOS M-series:** prefers `memory_pressure` CLI over raw RAM % for unified memory
- **Auth:** No auth for `127.0.0.1`; `X-API-Key` required when bound to `0.0.0.0`
- **Default bind:** `127.0.0.1`; LAN is opt-in
- **Config:** `~/.llm-valet/config.yaml`; permissions enforced to `0600`
- **Service level:** User-level only; never requires root

## Code Comment Standard

A comment is required when any of the following cannot be recovered by reading the code alone:

- **Why** — the reason a choice was made, especially when the obvious alternative was intentionally rejected
- **Constraint** — something that must survive refactoring: security mitigation, compliance requirement, platform limitation
- **Contract** — a precondition, postcondition, or idempotency guarantee that a caller depends on
- **Workaround** — behavior that looks wrong but is correct due to an external API quirk, OS behavior, or versioned dependency
- **Intentional absence** — code that is deliberately not present and must stay absent

Do not comment on *what* the code does — only on what the code *cannot say about itself*.
If the comment would read as a translation of the code into English, delete it.

---

## Parked Features (Do Not Implement Yet)

All items below are deferred to post-v1.0. Priorities and version targets will be set after v1.0 ships.

### Additional Providers

- **LM Studio** — `providers/lmstudio.py`. LM Studio exposes an OpenAI-compatible REST API; the `LLMProvider` ABC is designed to accommodate it. No code written yet.
- **vLLM** — `providers/vllm.py`. Primarily Linux/server-focused; low priority until Linux platform is validated post-v1.0.
- **MLX (Apple Silicon)** — `providers/mlx.py`. Apple's MLX framework (`mlx-lm`) runs models natively on M-series GPU/Neural Engine without Ollama as a middleman. Natural macOS-only addition post-v1.0.

### Ollama Auto-Update (Homebrew installs only)

Design decisions locked. Implementation home: `updater.py`.

- **Scope:** Homebrew installs only. Ollama.app has its own Sparkle updater; Homebrew has no equivalent — that's the gap.
- **Version check:** `brew info ollama --json` — not GitHub API (avoids rate limits; handles Homebrew formula lag naturally). Check cached daily.
- **Privacy opt-out:** `check_for_updates: false` disables all outbound calls.
- **Two config knobs (both default off):** `auto_update_ollama`, `refresh_homebrew_before_update`
- **Pre-flight before showing update UI:** `which brew` succeeds → `brew list ollama` confirms Homebrew manages it → `brew list --pinned | grep ollama` (abort if pinned) → no dual-install collision.
- **Update window:** Surface offer during natural pause events (game detected, resource pressure).
- **Update flow:** drain active inference → pause → stop → `brew upgrade ollama` → verify binary hash against Ollama GitHub release checksums → start → health check → resume on success.
- **Rollback:** `brew switch ollama <old_version>`. Binary-level guarantee only — model file compatibility not guaranteed across major versions; log a warning.
- **Headless safety:** notify via WebUI badge + log entry — never silently execute on a machine with no one watching.
- **Mandatory user confirmation** before any download — no silent auto-updates.

### Model Auto-Update

Ambiguity around what "updated" means for a model. Requires UI-prompted user confirmation. No timeline.
