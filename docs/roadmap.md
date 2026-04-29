# llm-valet — Roadmap

Last updated: 2026-04-29 (v0.5.5 tour + security fix merged)

---

## Current state — v0.5.6

Fully functional on macOS (Apple Silicon + Intel) with Ollama as the provider.

| Capability | Status |
|---|---|
| Pause/resume via keep_alive | ✅ |
| Force-pause (kills inference runner) | ✅ |
| Auto-pause on RAM / CPU / GPU VRAM pressure | ✅ |
| Game detection (Steam / steamapps/common) | ✅ |
| Watchdog FSM with grace period + hysteresis | ✅ |
| REST API + WebUI dashboard | ✅ |
| First-run setup modal + API key generation | ✅ |
| Context window preserved across pause/resume | ✅ |
| Model management (list, load, pull, delete) | ✅ |
| Disk space guard before model pull | ✅ |
| Overcommit detection at startup | ✅ |
| macOS service manager (launchctl, both Ollama variants) | ✅ |
| Security: T1–T8 threat model implemented | ✅ |
| API key timing-safe comparison (hmac.compare_digest) | ✅ |
| Unit test suite: 378 tests, 98% coverage | ✅ |
| Linux / Windows resource collectors | ✅ (untested on hardware) |
| Linux / Windows service managers | ✅ (untested on hardware) |

---

## v0.6.0 — Docs, validation, and PyPI

**Gate:** All items below complete before promoting to v1.0.

| Item | Notes |
|---|---|
| Pre-v0.6.0 code review complete | ✅ All findings resolved |
| Bug fixes from review (B1–B2, M1–M3, L1–L4) | ✅ Commits 8abbac4, df17f29, d8a42a5 |
| Integration test harness for api.py, watchdog.py, ollama.py | Requires live Ollama instance |
| README complete — install, configure, operate | — |
| GitHub Wiki: Architecture + Module reference | — |
| User tour (first-run experience walkthrough) | — |
| End-to-end validation on Mac Mini (upgrade + clean install) | — |
| PyPI publish | — |
| SECURITY.md | ✅ |
| dev-rig CI integration (reusable workflows, pre-commit) | ✅ Merged PR #7 |
| Architecture code tour (CodeTour, 13 steps) | ✅ Merged PR #8 |

---

## v1.0 — macOS + Ollama, production-ready

**Definition:** v1.0 ships when v0.6.0 gate passes and the Mac Mini end-to-end test confirms a clean upgrade and clean install both work against a published PyPI package.

**Scope:** macOS only, Ollama only. No new features beyond what ships in v0.6.0.

---

## Post-v1.0 backlog

Priorities and version numbers for post-v1.0 work will be set after v1.0 ships. Items below have no committed order.

### Platform expansion

| Item | Notes |
|---|---|
| Linux platform testing + CI | systemd user service; LinuxResourceCollector hardware validation |
| Windows platform testing + CI | Windows Service (sc.exe); WindowsResourceCollector + WMI hardware validation |
| Windows config ACL enforcement | icacls — chmod(0600) is macOS/Linux only |

### Additional providers

| Provider | Notes |
|---|---|
| MLX (Apple Silicon) | mlx-lm — runs models natively on M-series without Ollama; macOS-only |
| LM Studio | OpenAI-compatible REST API; LLMProvider ABC already supports it |
| vLLM | Primarily Linux/server; depends on Linux platform validation first |

### GPU monitoring extras

| Extra | Notes |
|---|---|
| NVIDIA (pynvml) | Code integrated in resource collectors; needs hardware validation |
| AMD ROCm (pyrsmi) | Linux only; depends on Linux platform |
| Intel Arc (level-zero) | Linux + Windows |
| Qualcomm Snapdragon X | Windows Copilot+ PCs |
| DirectML (fallback) | Any DirectX 12 GPU on Windows |

### Features

| Feature | Notes |
|---|---|
| Ollama auto-update (Homebrew) | Design locked in CLAUDE.md Parked Features; `updater.py` |
| Rate limiter (distributed) | Current in-memory limiter is single-worker only |
| HTTPS / TLS | Recommend reverse proxy (nginx) for now; may add built-in option |
| Model auto-update | Requires clear UX design; ambiguous what "updated" means for a model |
