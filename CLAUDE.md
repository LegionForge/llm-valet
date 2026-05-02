# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Release Checklist (Non-Negotiable)

Before tagging any release, work through this sequence in order. Do not push the tag until all doc steps are complete — a release without a wiki sync is an incomplete release.

### 1. Code gate
- [ ] All unit tests passing (`pytest tests/unit/`)
- [ ] All integration tests passing (`pytest -m integration tests/integration/`)
- [ ] Static analysis clean (ruff, bandit, mypy)
- [ ] Version bumped in `pyproject.toml` and `llm_valet/api.py` (`_VERSION`)

### 2. Documentation hierarchy review (do this before merging to main)

Documentation has four levels. Changes flow downward — never edit a lower level without checking the level above it first.

```
L0  Code          — ultimate truth; everything else describes it
 ↓
L1  CLAUDE.md     — internal/AI-facing; dense and precise
 ↓
L2  docs/wiki/    — canonical USER-FACING source (edit here, not in the wiki directly)
 ↓
L3  GitHub Wiki   — synced FROM L2 (never edited directly)
    README.md     — user-facing summary drawn from L2
    CodeTour      — references L0 directly; validated separately
```

**Drift check: L0 → L1 (code → CLAUDE.md)**

```bash
git diff v<prev>..HEAD -- llm_valet/
```
For each changed file, verify its corresponding CLAUDE.md section still describes it accurately.

**Drift check: L1 → L2 (CLAUDE.md → docs/wiki/)**

| L1 section in CLAUDE.md | L2 file | Section to verify |
|---|---|---|
| Architecture diagram + component table | `docs/wiki/Architecture.md` | Component Overview |
| Watchdog FSM tick logic | `docs/wiki/Architecture.md` | Watchdog FSM |
| ThresholdEngine interface | `docs/wiki/Architecture.md` | ThresholdEngine |
| Security model T1–T8 | `docs/wiki/Architecture.md` | Security Model |
| API Endpoints table | `docs/wiki/Module-Reference.md` | api.py Endpoint Reference |
| Provider ABC | `docs/wiki/Module-Reference.md` | providers/base.py |
| ResourceCollector ABC + dataclasses | `docs/wiki/Module-Reference.md` | resources/base.py |
| svcmgr platform notes | `docs/wiki/Module-Reference.md` | svcmgr/ |

**If L2 content changed:** bump `Applies to` line to new version + today's date.
**If L2 content unchanged:** bump `Applies to` anyway — it signals "verified current at vX.Y.Z".

**Drift check: L0 → CodeTour**

The tour's 13 step line numbers must point at the right code. Run the validation script:
```bash
python3 -c "
import json, pathlib
tour = json.loads(pathlib.Path('.tours/architecture.tour').read_text())
for i, step in enumerate(tour['steps'], 1):
    f = pathlib.Path(step.get('file',''))
    ln = step.get('line', 0)
    exists = f.exists()
    print(f'Step {i:2d}: {\"OK\" if exists else \"MISSING\":7s} {f}:{ln}')
"
```
If any step is MISSING or points to the wrong construct, update the tour before tagging.

**README.md:** confirm the API table, install instructions, and feature list still match reality. README is a summary — if detail changed in L2, update the README summary to match.

### 3. Roadmap and changelog
- [ ] `docs/roadmap.md` current state updated to new version
- [ ] Completed items marked ✅, next milestone defined
- [ ] GitHub Release notes drafted (`gh release create v<version> --generate-notes`, then edit)

### 4. Wiki sync
```bash
cd /tmp && rm -rf llm-valet-wiki
git clone https://github.com/LegionForge/llm-valet.wiki.git llm-valet-wiki
cp docs/wiki/*.md llm-valet-wiki/
cd llm-valet-wiki
git add . && git commit -m "docs: sync wiki to v<version>"
TOKEN=$(gh auth token)
git remote set-url origin "https://jp-cruz:${TOKEN}@github.com/LegionForge/llm-valet.wiki.git"
git push origin master
```

### 5. Release
```bash
git rebase origin/main          # resolve divergence before PR
gh pr merge <PR#> --merge       # merge dev → main
git checkout main && git pull
git tag v<version>
git push origin v<version>      # triggers publish.yml → PyPI
gh release create v<version> --generate-notes
```

---

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
    vram_total_mb: int | None
    vram_used_mb: int | None
    vram_used_pct: float | None
    compute_pct: float | None

@dataclass
class DiskMetrics:
    path: str                   # monitored mount point ("/" or "C:\\")
    total_mb: int
    used_mb: int
    free_mb: int
    used_pct: float

@dataclass
class SystemMetrics:
    memory: MemoryMetrics
    cpu: CPUMetrics
    gpu: GPUMetrics
    disk: DiskMetrics
    timestamp: float

class ResourceCollector(ABC):
    @abstractmethod
    def collect(self) -> SystemMetrics: ...

    @abstractmethod
    def supported_metrics(self) -> set[str]: ...
    # e.g. {"memory", "cpu", "gpu", "pressure", "disk"}
    # Callers check this before trusting Optional fields

    def collect_disk(self) -> DiskMetrics: ...
    # Concrete base implementation — cross-platform via psutil.disk_usage().
    # No need to override in platform subclasses.
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
    ram_resume_pct: float = 60.0       # must be < ram_pause_pct — hysteresis gap
    cpu_pause_pct: float = 90.0
    cpu_sustained_seconds: int = 30    # must exceed threshold for this long before acting
    gpu_vram_pause_pct: float = 85.0
    pause_timeout_seconds: int = 120   # grace period before resume after resource clears
    check_interval_seconds: int = 10
    auto_resume_on_ram_pressure: bool = True
    # When False: RAM-triggered pauses require manual /resume — prevents oscillation
    # on machines where the model itself is the dominant RAM consumer.

class ThresholdEngine:
    def __init__(self, thresholds: ResourceThresholds): ...
    def evaluate(self, metrics: SystemMetrics) -> tuple[bool, str]:
        # Returns (should_pause, reason). Caller tracks CPU sustained-seconds externally.
        # RAM and GPU trigger immediately; CPU only signals True (caller counts ticks).
        ...
    def evaluate_resume(self, metrics: SystemMetrics) -> tuple[bool, str]:
        # Returns (safe_to_resume, reason).
        # All metrics must be below resume thresholds (RAM uses ram_resume_pct for hysteresis).
        ...
```

### Provider Interface (`providers/base.py`)

```python
@dataclass
class ProviderStatus:
    running: bool
    model_loaded: bool
    model_name: str | None
    memory_used_mb: int | None
    size_vram_mb: int | None = None          # VRAM portion (Ollama /api/ps size_vram)
    loaded_context_length: int | None = None # active context window (Ollama /api/ps)

@dataclass
class ModelInfo:
    name: str
    size_mb: int
    context_length: int | None

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
    async def force_pause(self) -> bool: ...  # evict model regardless of active requests
    @abstractmethod
    async def status(self) -> ProviderStatus: ...
    @abstractmethod
    async def health_check(self) -> bool: ...
    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...
    @abstractmethod
    async def load_model(self, model_name: str, num_ctx: int | None = None) -> bool: ...
    @abstractmethod
    async def delete_model(self, model_name: str) -> bool: ...
    @abstractmethod
    async def pull_model(self, model_name: str) -> bool: ...
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
        # Loops every check_interval_seconds, delegating all logic to _tick().

    async def stop(self) -> None: ...

    def notify_manual_pause(self) -> None:
        # Called by the API after a successful manual /pause.
        # Syncs watchdog state so the auto-resume grace period starts from now.
        # The API drives the provider call; this method syncs the watchdog view.

    def notify_manual_resume(self) -> None:
        # Called by the API after a successful manual /resume.
        # Bypasses evaluate_resume() — model is already loaded, no room-check needed.
```

**`_tick()` logic (called every interval):**

1. Health probe: `provider.health_check()` — if unhealthy → transition to `PROVIDER_DOWN`; recover passively on next tick when probe succeeds again.
2. `collector.collect()` → `SystemMetrics`
3. `_detect_game()` → scans psutil for exe paths containing `steamapps/common`
4. `ThresholdEngine.evaluate(metrics)` → `(resource_pressure, resource_reason)`
5. CPU sustained-seconds: accumulate `_cpu_pressure_ticks` while CPU threshold is breached; only count as triggered when `ticks * interval >= cpu_sustained_seconds`
6. `should_pause = game_detected OR (resource_pressure AND sustained)`
7. If `RUNNING` and `should_pause`: record `_pause_trigger` (`"ram"` / `"cpu"` / `"gpu"` / `"game"`) → `_transition_to_paused(reason)`
8. If `PAUSED` and not `should_pause`:
   a. `ThresholdEngine.evaluate_resume(metrics)` → `(safe_to_resume, resume_reason)` — called first
   b. If `_pause_trigger == "ram"` and `auto_resume_on_ram_pressure is False` → return (require manual `/resume`)
   c. If `safe_to_resume` and grace period elapsed → `_transition_to_running(resume_reason)`

State machine: `RUNNING → PAUSING → PAUSED → RESUMING → RUNNING` (plus `PROVIDER_DOWN` for unexpected provider exits). Every transition logs a structured reason string (e.g., `"RAM 87% >= 85% threshold"` or `"game detected — steamapps/common/Hades"`).

### API Endpoints

| Method | Path | Action |
|--------|------|--------|
| GET | `/status` | Provider state + current resource snapshot |
| GET | `/watchdog` | Watchdog state, last reason, pause trigger |
| GET | `/metrics` | Live `SystemMetrics` from `ResourceCollector` |
| POST | `/pause` | Manual pause (graceful) |
| POST | `/pause/force` | Force-evict model regardless of active requests |
| POST | `/resume` | Manual resume |
| GET | `/models` | List models available in the provider |
| POST | `/load` | Load a specific model by name |
| DELETE | `/models/{model_name}` | Delete a model from the provider |
| POST | `/models/pull` | Pull a model from the provider registry |
| POST | `/start` | Full service start |
| POST | `/stop` | Graceful service shutdown |
| POST | `/stop/force` | Immediate service shutdown (no drain) |
| POST | `/restart` | stop → sleep(2) → start |
| GET | `/config` | Read current thresholds + watchdog settings |
| PUT | `/config` | Update thresholds at runtime (persisted to config.yaml) |
| GET | `/docs` | Auto-generated OpenAPI docs (framework-generated) |

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

### Ollama Environment Configuration via llm-valet

**Decision — jp@legionforge.org, 2026-04-28. Post-v1.0.**

Ollama's runtime environment (e.g. `OLLAMA_HOST`, `OLLAMA_FLASH_ATTENTION`, `OLLAMA_KV_CACHE_TYPE`, context window defaults) is currently configured only in the Homebrew plist or system environment — llm-valet reads none of it and exposes none of it in the UI.

Future direction: allow reading and writing Ollama's environment variables from the llm-valet UI and `config.yaml`, then injecting them into the plist (or the process environment on non-launchd platforms) on start. This would make llm-valet the single control surface for both lifecycle and Ollama configuration — particularly valuable in multi-machine deployments where per-machine tuning (KV cache type, host binding, flash attention) would otherwise require SSHing into each machine to edit plists.

Implementation notes when revisited:
- On macOS Homebrew: read/write `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist` `EnvironmentVariables` dict
- On Linux systemd: read/write `~/.config/systemd/user/ollama.service` `[Service]` `Environment=` lines
- On Windows: registry or service wrapper env block
- `OLLAMA_HOST` changes require a service restart to take effect — surface this clearly in the UI
- Never overwrite keys the user has set manually without confirmation

### Model Auto-Update

Ambiguity around what "updated" means for a model. Requires UI-prompted user confirmation. No timeline.

### Pressure-Level Pause Trigger

**Decision — jp@legionforge.org, 2026-04-27. Do not implement before v0.6.0.**

`PressureLevel` is currently informational only — it is collected and reported in `/metrics` but never used as a pause trigger. The reason: loading a model on Apple Silicon routinely produces transient `CRITICAL` pressure readings, so using CRITICAL as a trigger would pause the service on every model load.

The right fix is a sustained-window approach matching the CPU `sustained_seconds` pattern: add `pressure_critical_sustained_seconds: int` to `ResourceThresholds`, accumulate ticks in the watchdog via `_pressure_critical_ticks`, and gate on `"pressure" in collector.supported_metrics()` to avoid behavior divergence on Windows (which derives `PressureLevel` from RAM% thresholds rather than a native OS signal).

Deferred because: (1) the current design is tested and working; (2) the empirical loading-transient duration on M-series hardware has not been measured, so a safe default cannot be set; (3) cross-platform pressure parity is not validated.

When revisiting: measure how long CRITICAL pressure lasts during `keep_alive=-1` model loading on the M4 Mac Mini test hardware. That floor determines the minimum safe `pressure_critical_sustained_seconds` default.
