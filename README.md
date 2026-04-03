# llm-valet

> Cross-platform drop-in utility that manages Ollama (and other LLM providers) lifecycle based on manual control or automatic resource/activity sensing.

**Platforms:** macOS · Windows · Linux

---

## What It Does

llm-valet watches your machine in real time. When a game launches, or RAM/CPU/GPU pressure spikes, it automatically unloads the LLM model from memory — then quietly reloads it when resources free up. A REST API and web dashboard give you full manual control at any time.

### Origin Use Case

A Mac Mini M4 doubles as both a persistent LLM server and a gaming machine. The valet detects when gaming is happening (or resources are scarce) and gracefully unloads the model and optionally the LLM service, then reloads when resources free up.

---

## Why This Exists

A thorough search of existing tools (April 2026) confirmed this fills a real gap. No existing project combines:

- Automatic pause/resume based on real-time resource pressure thresholds
- Gaming activity detection (Steam native process watching)
- Cross-platform REST API + web dashboard with manual override
- Provider abstraction (Ollama, LM Studio, vLLM)

| Nearest neighbor | Why it doesn't overlap |
|---|---|
| **Open WebUI** (130k+ stars) | Chat UI only — no lifecycle control, no resource management |
| **EnviroLLM** | Energy/resource benchmarking — monitoring only, not automatic control |
| **OllamaMan / ollama-dashboard** | Read-only dashboards — no pause/resume, no thresholds |
| **Ollama built-in `keep_alive`** | Time-based idle unload only — no resource pressure sensing, no gaming detection |

The GitHub issue [ollama/ollama#11085](https://github.com/ollama/ollama/issues/11085) documents community demand for resource-pressure-based unloading that Ollama has not implemented.

---

## Core Concepts

### Pause vs. Stop

| Action | Effect | Speed | When |
|---|---|---|---|
| **Pause / Resume** | Unloads model from memory; service stays running | Fast (seconds) | Default — resource pressure or game detected |
| **Stop / Start** | Full service shutdown via platform service manager | Slow (30–90s) | Maintenance or zero-memory-footprint |

### Supported Providers

| Provider | Status |
|---|---|
| Ollama | ✅ Implemented |
| LM Studio | 🔜 Planned |
| vLLM | 🔜 Planned |

---

## Architecture

```
llm_valet/
├── api.py              # FastAPI — HTTP endpoints + security middleware
├── watchdog.py         # Auto-mode: process watcher + resource signal consumer
├── config.py           # Settings loader (config.yaml or env vars)
├── providers/          # LLM provider abstraction
│   ├── base.py         #   LLMProvider ABC + ProviderStatus
│   └── ollama.py       #   Ollama implementation
└── resources/          # Machine resource monitoring abstraction
    ├── base.py         #   ResourceCollector ABC + ThresholdEngine (pure logic)
    ├── macos.py        #   Apple Silicon: unified memory pressure + Metal GPU
    ├── linux.py        #   psutil + pynvml / ROCm
    └── windows.py      #   psutil + WMI + pynvml
```

**`ThresholdEngine`** is pure logic — no I/O. Takes `SystemMetrics` + `ResourceThresholds`, returns `(should_pause: bool, reason: str)`. Fully unit-testable without mocking any OS APIs.

---

## API

| Method | Path | Action |
|---|---|---|
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

---

## Security

Binding to `0.0.0.0` requires an `X-API-Key` header. Default bind is `127.0.0.1` (no auth required locally).

Additional mitigations: `TrustedHostMiddleware` (DNS rebinding), strict CORS (no wildcard), `subprocess` with `shell=False` (command injection), `textContent`-only WebUI (XSS), provider URL validation (SSRF), user-level services only (privilege escalation).

---

## Quick Start

```bash
pip install llm-valet

# Run (localhost only — default, no auth required)
uvicorn llm_valet.api:app --host 127.0.0.1 --port 8765

open http://localhost:8765        # WebUI
open http://localhost:8765/docs   # API docs
```

```bash
# Manual control
curl http://localhost:8765/status
curl -X POST http://localhost:8765/pause
curl -X POST http://localhost:8765/resume

# LAN access (X-API-Key required)
curl -H "X-API-Key: your-key" -X POST http://mac-mini.local:8765/pause
```

Config lives at `~/.llm-valet/config.yaml`.

---

## Development

```bash
git clone https://github.com/LegionForge/llm-valet
cd llm-valet
pip install -e ".[dev]"

# Run with hot-reload
uvicorn llm_valet.api:app --host 127.0.0.1 --port 8765 --reload
```

Requirements: Python 3.11+ · fastapi · uvicorn · httpx · psutil · pyyaml  
Optional: `pynvml` for NVIDIA GPU metrics on Linux/Windows

---

## License

MIT License — Copyright (c) 2026 [LegionForge](https://github.com/LegionForge) · jp@legionforge.org

Attribution required: all copies and distributions must include the above copyright notice per the MIT license terms.
