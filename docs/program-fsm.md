# llm-valet — Program Lifecycle & State Diagram

This document covers the **valet binary** itself — what happens from `llm-valet` invocation through
shutdown. For the watchdog's internal state machine (RUNNING / PAUSED / PROVIDER_DOWN / …), see
[watchdog-fsm.md](watchdog-fsm.md).

---

## The Three Layers

llm-valet has three concurrent state machines at different abstraction levels. Understanding which
layer you are in prevents confusion when debugging.

| Layer | What it tracks | Where documented |
|-------|---------------|-----------------|
| **Program** | valet process lifecycle — BOOTING → RUNNING → STOPPING | This file |
| **Watchdog** | auto-pause/resume decisions — RUNNING / PAUSED / PROVIDER_DOWN / … | watchdog-fsm.md |
| **Provider** | Ollama itself — model loaded / unloaded / service stopped | Ollama docs |

The watchdog runs *inside* the program's RUNNING state. The provider is an external process that
the watchdog controls.

---

## Program State Diagram

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  llm-valet start / uvicorn invocation                               │
  └──────────────────────────────┬──────────────────────────────────────┘
                                 │
                                 ▼
               ┌─────────────────────────────────┐
               │           BOOTING               │  (transient)
               │                                 │
               │  1. root-user check             │
               │  2. load config.yaml            │
               │  3. init rotating JSON log      │
               │  4. select provider (Ollama…)   │
               │  5. select ResourceCollector    │
               │     (macOS / Linux / Windows)   │
               │  6. wire security middleware    │
               │  7. bind API port               │
               │  8. overcommit check (startup   │
               │     warning if model loaded >   │
               │     RAM pause threshold)        │
               │  9. launch watchdog task        │
               └──────────┬──────────────────────┘
                          │
            ┌─────────────┴──────────────┐
            │ failure paths              │ success
            ▼                            ▼
  ┌──────────────────────┐    ╔═══════════════════════════════════════════╗
  │       ABORTED        │    ║                RUNNING                    ║
  │                      │    ║                                           ║
  │  Hard exits — no     │    ║  API accepts requests.                   ║
  │  recovery:           │    ║  Watchdog loop ticks every N seconds.    ║
  │                      │    ║                                           ║
  │  • running as root   │    ║  ┌─────────────────────────────────────┐ ║
  │  • port bind fails   │    ║  │        Watchdog sub-FSM             │ ║
  │    (address in use   │    ║  │                                     │ ║
  │    or permission     │    ║  │  RUNNING ⇄ PAUSING ⇄ PAUSED        │ ║
  │    denied)           │    ║  │  RESUMING ⇄ RUNNING                 │ ║
  │                      │    ║  │  RUNNING / PAUSED → PROVIDER_DOWN   │ ║
  │  Soft warnings —     │    ║  │                                     │ ║
  │  valet continues:    │    ║  │  See watchdog-fsm.md                │ ║
  │                      │    ║  └─────────────────────────────────────┘ ║
  │  • corrupt config    │    ║                                           ║
  │    (falls back to    │    ║  Provider starts/stops independently:    ║
  │    defaults)         │    ║  /start, /stop, /restart change Ollama  ║
  │  • config perms too  │    ║  but NOT the valet process itself.       ║
  │    open (logs warn)  │    ║                                           ║
  │  • provider not      │    ║◄── SIGTERM / SIGINT (Ctrl-C, launchd    ║
  │    reachable at      │    ║    stop, systemd stop, kill)            ║
  │    startup (watchdog │    ║◄── uvicorn shutdown signal              ║
  │    will retry)       │    ╚════════════════════╤══════════════════════╝
  └──────────────────────┘                         │
                                                   ▼
                                    ┌──────────────────────────────┐
                                    │          STOPPING            │  (transient)
                                    │                              │
                                    │  1. watchdog.stop() called   │
                                    │     (sets internal stop flag)│
                                    │  2. watchdog asyncio task    │
                                    │     cancelled                │
                                    │  3. "llm-valet shutting      │
                                    │     down" logged             │
                                    │                              │
                                    │  Note: provider (Ollama) is  │
                                    │  NOT stopped here — it keeps │
                                    │  running. Only the valet     │
                                    │  process exits.              │
                                    └──────────────┬───────────────┘
                                                   │
                                                   ▼
                                    ┌──────────────────────────────┐
                                    │           STOPPED            │
                                    │    (process exits, code 0)   │
                                    └──────────────────────────────┘
```

---

## BOOTING — Step by Step

| Step | Code location | What can fail |
|------|--------------|---------------|
| Root check | `api.py:_check_not_root()` | `sys.exit()` if `os.getuid() == 0` — hard abort |
| Config load | `config.py:load_settings()` | Corrupt YAML → warning + defaults; missing file → defaults |
| Log init | `api.py:_configure_logging()` | Log dir creation fails → unhandled (would abort) |
| Provider select | `api.py:_build_provider()` | Unknown provider name → KeyError |
| Collector select | `api.py:_build_collector()` | Platform not recognized → falls through to Windows collector |
| Port bind | uvicorn internals | `OSError: [Errno 98] Address already in use` → process exits |
| Overcommit check | `api.py:lifespan()` | Provider unreachable → DEBUG log, skipped, continues |
| Watchdog start | `asyncio.create_task(watchdog.run())` | Task created; first tick fires after `check_interval_seconds` |

---

## RUNNING — What You Can Control via API

When the program is in RUNNING state, the API controls two different things:

**Watchdog behavior** (controls the watchdog sub-FSM):

| Endpoint | Effect on watchdog |
|----------|-------------------|
| `POST /pause` | Sets watchdog state to PAUSED directly — bypasses PAUSING |
| `POST /resume` | Sets watchdog state to RUNNING directly — bypasses RESUMING |

**Provider lifecycle** (controls Ollama — does NOT affect the valet process):

| Endpoint | Effect on Ollama |
|----------|----------------|
| `POST /start` | Starts the Ollama service (launchd / systemd / sc.exe) |
| `POST /stop` | Stops the Ollama service gracefully |
| `POST /restart` | stop → 2s delay → start |

> These return immediately. The action runs in the background — poll `/status` for the result.

---

## STOPPING — What Does and Does Not Happen

When the valet process receives SIGTERM (from launchd, systemd, `kill`, or Ctrl-C):

- **Does stop:** the watchdog loop, the FastAPI/uvicorn server, the valet process
- **Does NOT stop:** Ollama itself — it keeps serving requests after valet exits

This is intentional: valet is a lifecycle manager, not the provider. If you want Ollama stopped
before the valet exits, call `POST /stop` first, then stop the valet process.

---

## Relationship to the Watchdog FSM

```
  Program: RUNNING
  │
  └── Watchdog: RUNNING ──────────────────────────────────────────────────┐
      │                                                                    │
      │  [game detected OR RAM/CPU/GPU spike]                             │
      ▼                                                                    │
  Watchdog: PAUSING                                                        │
      │                                                                    │
      │  [pause() succeeds]           [pause() fails]                     │
      ▼                               ▼                                    │
  Watchdog: PAUSED              Watchdog: RUNNING ───────────────────────►│
      │                                                                    │
      │  [all clear + grace elapsed]                                       │
      ▼                                                                    │
  Watchdog: RESUMING                                                       │
      │                                                                    │
      │  [resume() succeeds]          [resume() fails]                    │
      ▼                               ▼                                    │
  Watchdog: RUNNING ──────────────────────────────────────────────────────┘
      │
      │  [health check fails]
      ▼
  Watchdog: PROVIDER_DOWN
      │
      │  [health check passes]
      ▼
  Watchdog: RUNNING
```

The watchdog loop runs for the entire duration of the program's RUNNING state. When the program
enters STOPPING, the watchdog task is cancelled mid-tick if necessary — no partial-tick state
is persisted.

---

## Config Reload

There is no `RELOADING` program state. `PUT /config` updates thresholds in memory and writes
`config.yaml` atomically. The running watchdog picks up the new thresholds on its next tick
without any restart.

The valet process must be restarted to pick up changes to: `host`, `port`, `provider`,
`ollama_url`, or `log_file`.

---

## First Run vs Regular Run

First run is not a separate program state — the process goes through the same BOOTING → RUNNING
path. The difference is what RUNNING looks like from the outside.

### What makes it a "first run"

`~/.llm-valet/config.yaml` does not exist yet. `load_settings()` returns all defaults:

| Setting | Default | Why it matters |
|---------|---------|---------------|
| `host` | `127.0.0.1` | localhost-only — no LAN exposure until you opt in |
| `port` | `8765` | standard port |
| `api_key` | `""` | empty — auth not enforced for localhost requests |
| `key_acknowledged` | `False` | **this is the first-run flag** |

`key_acknowledged: False` tells the WebUI to show the setup overlay. Once you acknowledge or
apply network config, it flips to `True` and is persisted to `config.yaml`. It never shows again.

### First-run flow

```
  [ install.sh / install.ps1 ]
           │
           │  copies files, registers launchd / systemd / sc service
           │
           ▼
  ┌──────────────────────────────────────────────────────┐
  │  BOOTING  (no config.yaml — all defaults)            │
  │                                                      │
  │  host=127.0.0.1  port=8765  key_acknowledged=False   │
  └────────────────────────┬─────────────────────────────┘
                           │
                           ▼
  ╔══════════════════════════════════════════════════════╗
  ║  RUNNING  [SETUP MODE]                               ║
  ║                                                      ║
  ║  API is live. Watchdog is running.                   ║
  ║  key_acknowledged = False                            ║
  ║                                                      ║
  ║  GET /setup  →  { needs_setup: true,                 ║
  ║                   api_key: "<key>" }   (localhost)   ║
  ║                                                      ║
  ║  The WebUI polls /setup on load and shows a          ║
  ║  setup overlay until the user completes one of       ║
  ║  the two flows below.                                ║
  ╚══════════════════════╤═══════════════════════════════╝
                         │
            ┌────────────┴──────────────┐
            │                           │
            ▼                           ▼
  ┌──────────────────────┐   ┌──────────────────────────────┐
  │  Path A — localhost  │   │  Path B — LAN / shared use   │
  │  only (default)      │   │                              │
  │                      │   │  Set host to 0.0.0.0 and     │
  │  POST /setup/        │   │  configure port in the WebUI │
  │  acknowledge         │   │                              │
  │                      │   │  POST /setup/apply           │
  │  key_acknowledged    │   │  { host, port }              │
  │  saved to disk.      │   │                              │
  │  No restart.         │   │  config.yaml written.        │
  │                      │   │  Process calls os._exit(0).  │
  └──────────┬───────────┘   │  launchd / systemd respawns. │
             │               └────────────┬─────────────────┘
             │                            │
             │                            │  BOOTING (config.yaml now exists,
             │                            │  key_acknowledged=True, host=0.0.0.0)
             │                            │
             └────────────────┬───────────┘
                              │
                              ▼
  ╔═══════════════════════════════════════════════════════╗
  ║  RUNNING  [NORMAL]                                    ║
  ║                                                       ║
  ║  key_acknowledged = True                              ║
  ║  GET /setup  →  { needs_setup: false, api_key: null } ║
  ║  Setup overlay never shown again.                     ║
  ║                                                       ║
  ║  All subsequent starts go directly here               ║
  ║  (config.yaml exists + key_acknowledged=True).        ║
  ╚═══════════════════════════════════════════════════════╝
```

### Security note on Path B (LAN setup)

`POST /setup/apply` and `POST /setup/acknowledge` are **localhost-only** — they reject requests
from any non-loopback IP with HTTP 403. This prevents a LAN client from prematurely dismissing
the setup modal or changing the bind address remotely.

The API key shown in `GET /setup` is returned **only once** — to a localhost caller, before
acknowledgment. After `key_acknowledged` flips to `True`, `/setup` returns `api_key: null`
regardless of who asks.

### Subsequent runs (the normal case)

Every run after the first is the same BOOTING → RUNNING sequence, but shorter:

```
  BOOTING
    │  config.yaml exists → load settings
    │  key_acknowledged=True → no setup check needed
    │  (all other BOOTING steps still run)
    ▼
  RUNNING [NORMAL]
    (setup overlay never appears)
```

The only way to re-trigger first-run behavior is to delete `~/.llm-valet/config.yaml`
(or set `key_acknowledged: false` in it manually).
