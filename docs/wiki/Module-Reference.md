# Module Reference

> **Applies to `v1.0.0`** — updated 2026-05-02

Public interfaces for every module in llm-valet. Internal helpers (prefixed `_`) are omitted unless they are part of a documented contract.

---

## `llm_valet/api.py`

### `create_app(settings)`

```python
def create_app(settings: Settings | None = None) -> FastAPI
```

Application factory. Constructs the provider and collector from `settings`, wires up the `Watchdog`, registers all security middleware, and attaches all route handlers. When `settings` is `None`, calls `load_settings()` automatically.

Called once at module level to produce the `app` singleton that uvicorn serves. Tests call it directly to inject a custom `Settings`.

**Middleware stack** (outermost to innermost):

| Layer | Purpose |
|-------|---------|
| `CORSMiddleware` | Blocks cross-origin requests not listed in `cors_origins` |
| Body size check (`@app.middleware`) | Rejects bodies > 64 KB before JSON parsing; handles chunked encoding |
| `TrustedHostMiddleware` | Rejects `Host` headers not in the allowlist (DNS rebinding mitigation) |
| `require_api_key` (per-route dep) | Enforces `X-API-Key` for non-localhost callers |

**Startup sequence** (via FastAPI `lifespan`):
1. Checks for root (`os.getuid() == 0` → immediate exit).
2. Configures JSON rotating-file logging and suppresses httpx/uvicorn access noise.
3. Runs an overcommit check: if a model is already loaded and its footprint exceeds `ram_pause_pct`, logs a structured warning.
4. Starts `watchdog.run()` as an asyncio task.

**Shutdown sequence**: calls `watchdog.stop()`, cancels the watchdog task.

### First-run setup flow (`/setup/*`)

Three endpoints handle first-run API key acknowledgment and network binding configuration. All three are localhost-only (`_is_local()` guard) and excluded from the OpenAPI schema.

| Path | Method | Description |
|------|--------|-------------|
| `/setup` | GET | Returns `{needs_setup, api_key}`. `api_key` is only included for localhost requests before `key_acknowledged` is set; after acknowledgment it is always `null`. |
| `/setup/acknowledge` | POST | Marks the key as seen; persists `key_acknowledged: true` to `config.yaml`. |
| `/setup/apply` | POST | Validates and applies a `{host, port}` change, persists to `config.yaml`, then triggers a graceful restart via `os._exit(0)` after a 1s delay so the HTTP response returns first. |

### `_is_local(request)`

```python
def _is_local(request: Request) -> bool
```

Returns `True` if the request client address is `127.0.0.1` or `::1`. Used to gate the `/setup/*` endpoints and the single-display of the generated API key.

### `require_api_key` dependency

```python
async def require_api_key(
    request: Request,
    x_api_key: Annotated[str, Header()] = "",
) -> None
```

FastAPI dependency injected via `Auth = Annotated[None, Depends(require_api_key)]`. Skips auth when client is `127.0.0.1` or `::1`. For all other origins, requires a non-empty `api_key` in config and validates it with `hmac.compare_digest` (constant-time). Returns HTTP 403 if `api_key` is not configured; HTTP 401 if the key does not match.

### Endpoint Reference

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | No | Serve `static/index.html` or fallback JSON if static dir absent |
| GET | `/status` | Yes | Provider state + resource snapshot + watchdog state + overcommit flag |
| GET | `/watchdog` | Yes | Watchdog FSM state and last transition reason |
| GET | `/metrics` | Yes | Live `SystemMetrics` from `ResourceCollector` |
| POST | `/pause` | Yes | Manual graceful pause (rate-limited: 2s cooldown) |
| POST | `/pause/force` | Yes | Force-evict model by killing runner processes then `keep_alive=0` |
| POST | `/resume` | Yes | Manual resume — pre-warm model (rate-limited: 2s cooldown) |
| GET | `/models` | Yes | List all locally available models |
| POST | `/load` | Yes | Load a specific model; unloads current model first if different |
| DELETE | `/models/{model_name}` | Yes | Delete a model from local storage |
| POST | `/models/pull` | Yes | Pull model from registry; requires 5 GB free disk (rate-limited: 5s) |
| POST | `/start` | Yes | Full service start via svcmgr; returns immediately (rate-limited: 3s) |
| POST | `/stop` | Yes | Graceful service shutdown via svcmgr; returns immediately (rate-limited: 3s) |
| POST | `/stop/force` | Yes | Kill runners then stop service; returns immediately (rate-limited: 3s, shared key with `/stop`) |
| POST | `/restart` | Yes | stop → 2s sleep → start; returns immediately (rate-limited: 3s) |
| GET | `/config` | Yes | Read current `ResourceThresholds` as JSON |
| PUT | `/config` | Yes | Partial threshold update; validates hysteresis invariant; persists to `config.yaml` |
| GET | `/docs` | No | Auto-generated OpenAPI UI (FastAPI default) |
| GET | `/setup` | No | First-run key display (localhost only) |
| POST | `/setup/acknowledge` | No | Mark key as seen (localhost only) |
| POST | `/setup/apply` | No | Apply host/port config and restart (localhost only) |

`start`, `stop`, `stop/force`, and `restart` return immediately with `{"ok": true, "action": "..."}` and complete the operation in a FastAPI `BackgroundTask`. Poll `/status` to observe the result.

---

## `llm_valet/watchdog.py`

### `WatchdogState`

```python
class WatchdogState(enum.Enum):
    RUNNING       = "running"
    PAUSING       = "pausing"
    PAUSED        = "paused"
    RESUMING      = "resuming"
    PROVIDER_DOWN = "provider_down"
```

### `Watchdog`

```python
class Watchdog:
    def __init__(
        self,
        provider: LLMProvider,
        collector: ResourceCollector,
        thresholds: ResourceThresholds,
    ) -> None
```

Constructs the watchdog with injected provider and collector. Never calls psutil or any platform API directly for resource data — all platform specifics are delegated to `collector`. Creates a `ThresholdEngine` from `thresholds`.

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `state` | `WatchdogState` | Current FSM state. Read by `api.py` for `/status` and `/watchdog`. |
| `last_reason` | `str` | Structured reason string from the most recent state transition. Examples: `"RAM 87.3% >= 85.0% threshold"`, `"game detected — steamapps/common/Hades"`, `"manual pause"`. |

#### Methods

```python
async def run(self) -> None
```
Main loop. Runs until `stop()` is called. Calls `_tick()` every `check_interval_seconds`. Catches and logs any exception from `_tick()` without stopping the loop.

```python
async def stop(self) -> None
```
Sets the running flag to False, causing `run()` to exit after the current sleep. Does not call `provider.pause()` — the provider is left in its current state.

```python
def notify_manual_pause(self) -> None
```
Called by `api.py` after a successful `POST /pause` or `POST /pause/force`. Sets state to `PAUSED`, records `_paused_at = time.monotonic()`, and sets `last_reason = "manual pause"`. Syncs the grace period clock so auto-resume behaves correctly after a manual pause.

```python
def notify_manual_resume(self) -> None
```
Called by `api.py` after a successful `POST /resume` or `POST /load`. Sets state to `RUNNING`, clears `_paused_at` and `_pause_trigger`, and sets `last_reason = "manual resume"`. Bypasses `evaluate_resume()` — the model is already loaded.

---

## `llm_valet/config.py`

### `Settings`

```python
@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    provider: str = "ollama"
    ollama_url: str = "http://127.0.0.1:11434"
    model_name: str | None = None
    api_key: str = ""
    key_acknowledged: bool = False
    cors_origins: list[str] = field(default_factory=list)
    extra_allowed_hosts: list[str] = field(default_factory=list)
    thresholds: ResourceThresholds = field(default_factory=ResourceThresholds)
    log_file: str = "~/.llm-valet/valet.log"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | `str` | `"127.0.0.1"` | Bind address. Use `"0.0.0.0"` for LAN access (requires `api_key`) |
| `port` | `int` | `8765` | Listen port |
| `provider` | `str` | `"ollama"` | Provider name; only `"ollama"` is supported in v1.0 |
| `ollama_url` | `str` | `"http://127.0.0.1:11434"` | Ollama base URL; validated to localhost or RFC1918 only (T6) |
| `model_name` | `str \| None` | `None` | Default model for pause/resume; `None` uses the currently loaded model |
| `api_key` | `str` | `""` | API key for LAN access; empty disables all non-localhost requests |
| `key_acknowledged` | `bool` | `False` | True after first-run setup flow completes |
| `cors_origins` | `list[str]` | `[]` | Explicit CORS origin allowlist; never `"*"` |
| `extra_allowed_hosts` | `list[str]` | `[]` | Additional hosts for `TrustedHostMiddleware` |
| `thresholds` | `ResourceThresholds` | (see below) | Pause/resume threshold configuration |
| `log_file` | `str` | `"~/.llm-valet/valet.log"` | Rotating JSON log file path (tilde-expanded) |

#### `Settings` methods

```python
def acknowledge_key(self) -> None
```
Sets `key_acknowledged = True` and persists to `config.yaml`.

```python
def apply_network_config(self, host: str, port: int) -> None
```
Updates `host` and `port`, sets `key_acknowledged = True`, and persists.

```python
def update_thresholds(self, data: dict[str, Any]) -> dict[str, Any]
```
Applies a partial threshold update dict. Validates all percentage fields are in `(0, 100]`, validates `check_interval_seconds >= 1`, enforces the `ram_resume_pct < ram_pause_pct` hysteresis invariant. Raises `ValueError` on any violation. Persists on success and returns the full updated threshold dict.

### `load_settings()`

```python
def load_settings() -> Settings
```

Loads `~/.llm-valet/config.yaml` if it exists, warns if the file is world-readable (group or other read bits set), applies YAML values, then applies env var overrides. Returns a `Settings` instance with defaults for any missing values. If `config.yaml` is corrupt YAML, logs an error and continues with defaults.

`ollama_url` from YAML is validated — scheme must be `http` or `https`, host must be `localhost`, `::1`, a `.local` mDNS name, or an RFC1918 address. Invalid values are logged and ignored.

### `_apply_env_overrides(settings)`

```python
def _apply_env_overrides(settings: Settings) -> None
```

Applies environment variable overrides after YAML loading. Env vars take precedence over `config.yaml`.

| Variable | Field | Notes |
|----------|-------|-------|
| `LLM_VALET_HOST` | `host` | Bind address override |
| `LLM_VALET_PORT` | `port` | Must be a valid integer; warning logged and default kept on parse failure |
| `LLM_VALET_API_KEY` | `api_key` | API key override |
| `LLM_VALET_PROVIDER` | `provider` | Provider name override |

---

## `llm_valet/providers/base.py`

### `ProviderStatus`

```python
@dataclass
class ProviderStatus:
    running: bool
    model_loaded: bool
    model_name: str | None
    memory_used_mb: int | None
    size_vram_mb: int | None = None
    loaded_context_length: int | None = None
```

| Field | Type | Description |
|-------|------|-------------|
| `running` | `bool` | True if the provider process is reachable |
| `model_loaded` | `bool` | True if a model is currently resident in memory |
| `model_name` | `str \| None` | Name of the loaded model, or `None` if none loaded |
| `memory_used_mb` | `int \| None` | Total memory used by the loaded model in MB (from `/api/ps size`) |
| `size_vram_mb` | `int \| None` | VRAM portion of model memory in MB (Ollama `/api/ps size_vram`) |
| `loaded_context_length` | `int \| None` | Active context window in tokens (Ollama `/api/ps context_length`) |

### `ModelInfo`

```python
@dataclass
class ModelInfo:
    name: str
    size_mb: int
    context_length: int | None
```

### `LLMProvider` ABC

```python
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
    async def force_pause(self) -> bool: ...

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

All methods return `bool` where `True` indicates success. `status()` returns a `ProviderStatus` that callers must not cache — it reflects live provider state at call time. `health_check()` is a lightweight liveness probe (no model state, 5s timeout); `status()` is a heavier call that includes model metadata via `/api/ps`.

---

## `llm_valet/providers/ollama.py`

### `OllamaProvider`

```python
class OllamaProvider(LLMProvider):
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model_name: str | None = None,
        request_timeout: float = 15.0,
    ) -> None
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | `str` | `"http://127.0.0.1:11434"` | Ollama API base URL (trailing slash stripped) |
| `model_name` | `str \| None` | `None` | Default model name; `None` triggers auto-detection via `/api/ps` |
| `request_timeout` | `float` | `15.0` | Default HTTP timeout in seconds for all Ollama API calls |

### Pause / Resume Mechanism

Both operations go through Ollama's `/api/generate` endpoint with `stream: false`:

- **Pause**: `POST /api/generate {model, keep_alive: 0, stream: false}` — Ollama evicts the model from memory and returns `done_reason: "unload"`. `stream: false` is required; without it Ollama sends a chunked response and only the first chunk is parsed, so `done_reason` is never seen.

- **Resume**: `POST /api/generate {model, keep_alive: -1, stream: false}` — Ollama pre-warms the model into memory. Uses a 60s timeout (model loading from slow storage can take tens of seconds). If `_last_loaded_ctx` was captured at pause time, it is restored via `options: {num_ctx: ...}`.

Before `pause()` sends `keep_alive: 0`, it calls `status()` to capture `loaded_context_length`. The `/api/ps` endpoint returns empty after eviction, so context length must be captured before the eviction call.

### `force_pause`

```python
async def force_pause(self) -> bool
```

Used when `pause()` is blocked by an active inference request. Sequence:

1. Call `status()` to capture model name and context length before the runner is killed.
2. Call `_kill_ollama_runners()` — finds and kills processes named `ollama_llama_runner` or Ollama binaries invoked with a `runner` subcommand (excluding `serve`). Returns the count killed. Uses `psutil.kill()` — no shell, no injection surface.
3. Sleep 500ms to let Ollama register the runner exit.
4. Call `pause()` (`keep_alive: 0`) regardless of whether any runner was killed — the keep_alive call signals Ollama to release the model slot and prevents auto-restart.

Falls back gracefully to regular `pause()` if no runner processes are found.

### `_resolve_model()`

```python
async def _resolve_model(self) -> str | None
```

Returns the model name to act on. Resolution order:

1. `self._model_name` (from config or constructor) — returned if set and passes name validation.
2. First model in `/api/ps` — the currently loaded model.
3. `self._last_loaded_model` — cached at pause time. `/api/ps` returns empty after eviction; this cache allows `resume()` to restore the right model.

Returns `None` if no model name can be determined. All names are validated against `^[a-zA-Z0-9:._-]{1,200}$` before use. When `pause()` gets `None` back from `_resolve_model()`, it skips silently and returns `True` (no model loaded is not an error).

### `load_model`

```python
async def load_model(self, model_name: str, num_ctx: int | None = None) -> bool
```

Serialized by `_load_lock` (asyncio.Lock) — concurrent `/load` calls are queued, not raced. Sequence: unload current model via `keep_alive: 0` if a different model is loaded, then pre-warm the new model via `keep_alive: -1`. Updates `_model_name` and `_last_loaded_model` on success.

`num_ctx` overrides Ollama's default context window. Must be `>= 512` if provided; silently ignored if below that floor.

---

## `llm_valet/resources/base.py`

### `PressureLevel`

```python
class PressureLevel(enum.Enum):
    NORMAL   = "normal"
    WARN     = "warn"
    CRITICAL = "critical"
```

On macOS, sourced from the `memory_pressure` CLI (OS-native signal for Apple Silicon unified memory). On Linux and Windows, derived from RAM% thresholds. Reported in `/metrics` for informational purposes. Not used as a pause trigger — loading a large model on Apple Silicon routinely produces transient CRITICAL readings even within a safe RAM% budget.

### Metric Dataclasses

#### `MemoryMetrics`

```python
@dataclass
class MemoryMetrics:
    total_mb: int
    used_mb: int
    used_pct: float
    pressure: PressureLevel
```

#### `CPUMetrics`

```python
@dataclass
class CPUMetrics:
    used_pct: float   # 1-second average
    core_count: int
```

#### `GPUMetrics`

```python
@dataclass
class GPUMetrics:
    available: bool           # False if no GPU driver accessible
    vram_total_mb: int | None
    vram_used_mb: int | None
    vram_used_pct: float | None
    compute_pct: float | None
```

Callers must check `available` before trusting any other field. On macOS M-series, GPU and CPU share unified memory — `vram_*` fields reflect the GPU portion of that unified pool.

#### `DiskMetrics`

```python
@dataclass
class DiskMetrics:
    path: str           # "/" on macOS/Linux, "C:\\" on Windows
    total_mb: int
    used_mb: int
    free_mb: int
    used_pct: float
```

Used to gate model pulls — `/models/pull` rejects requests when `free_mb < 5120` (5 GB).

#### `SystemMetrics`

```python
@dataclass
class SystemMetrics:
    memory: MemoryMetrics
    cpu: CPUMetrics
    gpu: GPUMetrics
    disk: DiskMetrics
    timestamp: float = field(default_factory=time.time)
```

Complete snapshot returned by `ResourceCollector.collect()`. `timestamp` is a Unix epoch float set at collection time.

### `ResourceCollector` ABC

```python
class ResourceCollector(ABC):
    @abstractmethod
    def collect(self) -> SystemMetrics: ...

    @abstractmethod
    def supported_metrics(self) -> set[str]: ...

    def collect_disk(self) -> DiskMetrics: ...
```

`collect()` returns a full `SystemMetrics` snapshot. `supported_metrics()` returns a set of strings indicating which fields are populated from real hardware data — e.g. `{"memory", "cpu", "gpu", "pressure", "disk"}`. Callers check this before trusting optional GPU fields on platforms where GPU data is unavailable.

`collect_disk()` is a concrete base implementation using `psutil.disk_usage()`. It is identical on macOS, Linux, and Windows and does not need to be overridden in platform subclasses.

### `ResourceThresholds`

```python
@dataclass
class ResourceThresholds:
    ram_pause_pct: float = 85.0
    ram_resume_pct: float = 60.0
    cpu_pause_pct: float = 90.0
    cpu_sustained_seconds: int = 30
    gpu_vram_pause_pct: float = 85.0
    pause_timeout_seconds: int = 120
    check_interval_seconds: int = 10
    auto_resume_on_ram_pressure: bool = True
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `ram_pause_pct` | `float` | `85.0` | RAM% at which to pause; must be > `ram_resume_pct` |
| `ram_resume_pct` | `float` | `60.0` | RAM% below which resume is allowed; hysteresis gap prevents oscillation |
| `cpu_pause_pct` | `float` | `90.0` | CPU% threshold for pause trigger |
| `cpu_sustained_seconds` | `int` | `30` | Seconds CPU must stay above threshold before pausing |
| `gpu_vram_pause_pct` | `float` | `85.0` | GPU VRAM% at which to pause; triggers immediately (no sustained window) |
| `pause_timeout_seconds` | `int` | `120` | Grace period in seconds after pressure clears before auto-resume |
| `check_interval_seconds` | `int` | `10` | Watchdog tick interval in seconds |
| `auto_resume_on_ram_pressure` | `bool` | `True` | When `False`, RAM-triggered pauses require manual `/resume` to prevent oscillation on machines where the model is the dominant RAM consumer |

The config layer enforces `ram_resume_pct < ram_pause_pct` at load time and on `PUT /config`. Violations are rejected with HTTP 400.

### `ThresholdEngine`

```python
class ThresholdEngine:
    def __init__(self, thresholds: ResourceThresholds) -> None
```

Pure logic, no I/O. Holds a reference to `ResourceThresholds` for threshold values.

```python
def evaluate(self, metrics: SystemMetrics) -> tuple[bool, str]
```

Returns `(should_pause, reason)`. Checks in order: RAM, CPU, GPU VRAM. Returns on the first breach. RAM and GPU trigger immediately; CPU always returns `True` when the threshold is exceeded — the watchdog caller tracks sustained-seconds externally. Reason strings are structured for log parsing, e.g. `"RAM 87.3% >= 85.0% threshold"`.

```python
def evaluate_resume(self, metrics: SystemMetrics) -> tuple[bool, str]
```

Returns `(safe_to_resume, reason)`. All metrics must be below their resume thresholds for this to return `True`. RAM uses `ram_resume_pct` (hysteresis); CPU and GPU use their respective pause thresholds (the grace period provides the sustained buffer on the resume side).

---

## `svcmgr/macos.py`

Manages the Ollama service on macOS. Handles two install variants. Detection checks filesystem paths at call time — no caching.

| Variant | Detection condition | Start mechanism | Stop mechanism |
|---------|---------------------|-----------------|----------------|
| App | `/Applications/Ollama.app` directory exists | `open -a Ollama` | `osascript -e 'quit app "Ollama"'`; falls back to psutil SIGTERM by exe path if osascript fails |
| Brew CLI | `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist` or `com.ollama.ollama.plist` exists | `launchctl bootstrap gui/<uid> <plist>` | `launchctl bootout gui/<uid>/<label>` |

```python
def start_service() -> bool
```
Detects the install variant and starts accordingly. Returns `False` if neither variant is found.

```python
def stop_service() -> bool
```
Stops and prevents automatic respawn. For the Brew variant, `"No such process"` in stderr is treated as success (already stopped). For the App variant, AppleScript failure falls back to psutil SIGTERM by matching the process exe path against `/Applications/Ollama.app/Contents/MacOS/Ollama`.

All subprocess calls use `shell=False`. The user domain (`gui/<uid>`) is used throughout — root is never required.

Supports both `homebrew.mxcl.ollama.plist` (current formula) and `com.ollama.ollama.plist` (older formula), checked in that order.

---

## `svcmgr/linux.py`

```python
def start_service() -> bool
```
Detection order:
1. If a systemd user unit `ollama.service` exists (`systemctl --user cat ollama.service` returns 0): runs `systemctl --user start ollama.service`.
2. Otherwise: spawns `ollama serve` as a detached background process (`start_new_session=True`) so it survives llm-valet restarts. Searches PATH then `/usr/local/bin/ollama`, `/usr/bin/ollama`, `~/.local/bin/ollama`.

```python
def stop_service() -> bool
```
If a systemd user unit exists: runs `systemctl --user stop ollama.service` and returns `True`.

If only a root-owned system service exists (the official Ollama Linux installer default) or Ollama is running as a bare process: returns `False`. `ollama.py` then handles termination via psutil SIGTERM / SIGKILL fallback. llm-valet never runs as root, so it cannot control root-owned system services directly.

---

## `svcmgr/windows.py`

```python
def start_service() -> bool
```
Detection order:
1. If a Windows Service named `"Ollama"` is registered (`sc query Ollama` returns 0): runs `sc start Ollama`.
2. Otherwise: launches the Ollama executable directly with `DETACHED_PROCESS | CREATE_NO_WINDOW` flags so the process survives if llm-valet's console window is closed. Checks `%LOCALAPPDATA%\Programs\Ollama\ollama.exe` first, then PATH.

```python
def stop_service() -> bool
```
If a Windows Service exists: runs `sc stop Ollama`. Treats error 1062 ("service not started") as success.

If Ollama is running as a tray application (the default for the official installer): returns `False`. `ollama.py` handles termination via psutil. The Windows Service path is included for enterprise deployments that register Ollama manually — it is not the default install.

All subprocess calls use `shell=False`.
