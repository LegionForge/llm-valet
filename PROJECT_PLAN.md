# llm-valet — Project Plan

## Current milestone: v0.1.0 — 2026-04-04

**Goal:** First end-to-end test on the Mac Mini M4. Confirm all 10 build steps
work together against a real Ollama instance.

### In scope for v0.1.0 testing

| Area | Status |
|---|---|
| `resources/base.py` — dataclasses, ThresholdEngine | Built ✓ |
| `providers/base.py` — LLMProvider ABC | Built ✓ |
| `resources/macos.py` — memory_pressure, psutil, ioreg GPU | Built ✓ |
| `providers/ollama.py` — pause, resume, start, stop, status, health_check | Built ✓ |
| `api.py` — FastAPI, all endpoints, security middleware | Built ✓ |
| `static/index.html` — WebUI | Built ✓ |
| `watchdog.py` — state machine, game detection, threshold integration | Built ✓ |
| `svcmgr/macos.py` — launchctl, both Ollama variants | Built ✓ |
| `resources/linux.py` + `resources/windows.py` | Built ✓ (tested on Mac only for now) |
| `install/install.sh` + `uninstall.sh` | Built ✓ |
| `install/install.ps1` + `uninstall.ps1` | Built ✓ (Windows — deferred test) |

**Testing target:** See `docs/testing/v0.1-mac-mini.md`

---

## Deferred — pending v0.1.0 test sign-off

### Ollama Updater (target: v1.0)

Full design workshopped 2026-04-04. Key decisions locked:

- **Scope:** macOS + Homebrew install only. Ollama .app has its own Sparkle updater; Homebrew installs have no equivalent — that's the gap llm-valet fills.
- **Version check:** `brew info ollama --json` — not GitHub API. Avoids rate limits; naturally handles Homebrew formula lag; check cached daily.
- **Privacy opt-out:** `check_for_updates: false` disables all outbound calls; user informed they control updates manually.
- **Two config knobs (both default off):**
  - `auto_update_ollama: false` — check but don't update without user action
  - `refresh_homebrew_before_update: false` — run `brew update` before `brew upgrade ollama`
- **Pre-flight checks before showing update UI:**
  1. `which brew` succeeds
  2. `brew list ollama` confirms Homebrew manages it
  3. `brew list --pinned | grep ollama` — abort if pinned
  4. Confirm no dual-install collision (Homebrew binary matches what's in PATH)
- **Update window:** Surface offer during natural pause events (gaming detected, resource pressure). Already stopped = minimal disruption.
- **Update flow:** drain active inference → pause → stop → `brew upgrade ollama` → verify binary hash against Ollama GitHub release checksums → start → health check → resume on success.
- **Rollback:** Homebrew keeps old version in cellar until `brew cleanup`. On failure: `brew switch ollama <old_version>`. Guarantee is binary-level only — model file compatibility not guaranteed across major versions; log a warning.
- **Headless safety:** On a headless system, "silent" means notify (WebUI badge + log entry), not execute without trace. Never silently execute on a machine with no one watching.
- **Post-upgrade functional verification:** `ollama --version` matches expected, health check passes API — only then declare success.
- **Implementation home:** `updater.py` module, separate from installer scripts.

### Windows + Linux testing (target: v0.2)

- Windows: Task Scheduler service registration, WindowsResourceCollector (WMI), install.ps1/uninstall.ps1
- Linux: systemd user service, LinuxResourceCollector (psutil + pynvml), install.sh/uninstall.sh Linux paths

### LM Studio + vLLM providers (target: v0.2)

Stubs exist in `providers/`. Full implementation deferred until core Ollama path is validated.

### WebUI visual polish (target: v0.2)

`static/index.html` exists. Functional testing in v0.1; visual/UX polish deferred.

### NVIDIA GPU monitoring (target: v0.2)

`pynvml` extra defined in pyproject.toml. Integration in `resources/linux.py` and `resources/windows.py` pending real hardware test.

---

## Parked — no timeline

### Provider auto-update (general)

See Ollama Updater section above for macOS/Homebrew. Other providers (LM Studio, vLLM) have no equivalent path identified yet.

### Model auto-update

Ambiguity around what "updated" means for a model. Requires UI-prompted user confirmation. No timeline.
