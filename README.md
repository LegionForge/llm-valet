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

## Static Analysis

Four tools run before every commit. All are in `[dev]` dependencies and configured in `pyproject.toml`.

Seven tools cover linting, security SAST, type safety, dependency CVEs, broader SAST, test coverage, and commit-time enforcement.

| Tool | Purpose | Runs |
|---|---|---|
| **Ruff** | Lint + import sort | pre-commit, CI |
| **Bandit** | Security SAST (Python patterns) | pre-commit, CI |
| **mypy** | Type checking (strict mode) | pre-commit, CI |
| **pip-audit** | Dependency CVE scan | CI |
| **semgrep** | Broader SAST (FastAPI + OWASP rulesets) | CI |
| **pytest-cov** | Test coverage (≥80% enforced) | CI |
| **pre-commit** | Runs ruff + bandit + mypy on every `git commit` | local |

### Installation and setup

Create a **project venv** — not your system Python or Anaconda. pip-audit scans installed packages; running it against Anaconda floods results with unrelated packages.

```bash
# From repo root
python -m venv .venv

# Activate — PowerShell
.venv\Scripts\Activate.ps1

# Activate — macOS / Linux / Git Bash
source .venv/bin/activate

# Install project + all dev tools
pip install -e ".[dev]" types-PyYAML
```

**Validate the install:**

```bash
python -m ruff --version        # expect: ruff 0.4.x or later
python -m bandit --version      # expect: bandit 1.7.x or later
python -m mypy --version        # expect: mypy 1.10.x or later
python -m pip_audit --version   # expect: pip-audit 2.7.x or later
python -m semgrep --version     # expect: semgrep 1.70.x or later
python -m pytest --version      # expect: pytest 8.x with cov plugin
pre-commit --version            # expect: pre-commit 3.7.x or later
```

If any command returns "not found", the venv is not active or the install failed. Re-run `pip install -e ".[dev]"` with the venv active.

**Install the git hook (one-time per clone):**

```bash
pre-commit install
```

After this, ruff + bandit + mypy run automatically on every `git commit`. A failed hook blocks the commit — fix the issue, re-stage, and commit again. To skip in an emergency: `git commit --no-verify` (use sparingly, log why).

**Validate the hook is installed:**

```bash
pre-commit run --all-files
```

All hooks should pass on a clean checkout.

---

### Running the tools

All commands run from the repo root with the venv active.

#### Ruff — linting and import sorting

```bash
python -m ruff check llm_valet svcmgr
```

Auto-fix safe issues (formatting, import order):

```bash
python -m ruff check llm_valet svcmgr --fix
```

**Reading the output:**

```
llm_valet/api.py:45:5: S105 Possible hardcoded password assigned to: "api_key"
llm_valet/watchdog.py:12:1: F401 `os` imported but unused
Found 2 errors.
```

Format: `file:line:col: CODE description`

| Code prefix | Category | Act on it? |
|---|---|---|
| `E`, `W` | Style / formatting | Yes — auto-fixable |
| `F` | Pyflakes (unused imports, undefined names) | Yes — real bugs |
| `I` | Import order | Yes — auto-fixable |
| `S` | Security (bandit-style) | Yes — read carefully |
| `B` | Bugbear (common bugs) | Yes |
| `UP` | Modernisation opportunities | Yes — auto-fixable |
| `RUF` | Ruff-specific checks | Yes |

**This project suppresses** `S603` (subprocess, shell=False reviewed) and `S607` (partial executable path for system binaries). Those skips are intentional — do not remove them.

Clean output:

```
All checks passed!
```

---

#### Bandit — security SAST

```bash
python -m bandit -r llm_valet svcmgr -c pyproject.toml
```

**Reading the output:**

```
>> Issue: [B324:hashlib] Use of weak MD5 hash for security.
   Severity: Medium   Confidence: High
   Location: llm_valet/config.py:45
   More Info: https://bandit.readthedocs.io/en/latest/plugins/b324_hashlib.html
```

Triage by the intersection of Severity and Confidence:

| | High Confidence | Medium Confidence | Low Confidence |
|---|---|---|---|
| **High Severity** | Fix immediately | Investigate | Review |
| **Medium Severity** | Investigate | Review | Low priority |
| **Low Severity** | Review | Low priority | Probably noise |

To see all findings including suppressed codes:

```bash
python -m bandit -r llm_valet svcmgr -c pyproject.toml --skips ""
```

**This project suppresses** `B404`, `B603`, `B607` — all subprocess-related, reviewed and confirmed safe because `shell=False` is enforced throughout.

Clean output:

```
Test results:
        No issues identified.
```

---

#### mypy — type checking

```bash
python -m mypy llm_valet svcmgr
```

**Reading the output:**

```
llm_valet/providers/ollama.py:145: error: Item "None" of "str | None" has no attribute "lower"  [union-attr]
llm_valet/config.py:68: error: Argument 1 to "setattr" has incompatible type  [arg-type]
Found 2 errors in 2 files (checked 8 source files)
```

Format: `file:line: error: description  [error-code]`

Error codes relevant to correctness and security:

| Code | What it means | Security relevance |
|---|---|---|
| `[union-attr]` | Used a value that could be `None` without a None-check | Potential crash / bypass |
| `[arg-type]` | Wrong type passed to a function | Logic error, silent failures |
| `[return-value]` | Function returns the wrong type | Silent data corruption |
| `[attr-defined]` | Attribute doesn't exist on the type | Likely a typo or wrong object |
| `[no-untyped-def]` | Function missing type annotations | Reduces audit coverage |

This project runs **strict mode** — all functions must be annotated, all `Optional` accesses checked. A clean run means the type system has verified the full call graph.

`# type: ignore[attr-defined]` comments in `svcmgr/macos.py` are intentional — `os.getuid()` is macOS-only and mypy runs on Windows in CI; the comment documents this rather than suppressing a real error.

Clean output:

```
Success: no issues found in N source files
```

---

#### pip-audit — dependency CVEs

```bash
python -m pip_audit
```

**Reading the output:**

```
Name          Version  ID                   Fix Versions
------------- -------- -------------------- ------------
cryptography  41.0.0   GHSA-jfh8-c2jp-x4fc  41.0.6
```

For each finding:

1. Read the advisory (the ID is a link when run with `--format=columns`)
2. Check whether the vulnerable code path is reachable from llm-valet's usage
3. Upgrade if a fix version exists: `pip install "cryptography>=41.0.6"`
4. If no fix version exists, check the advisory for mitigations

**Must run inside the project venv**, not a global Anaconda env. Anaconda installs many packages unrelated to this project and will produce many false-positive CVEs.

Clean output:

```
No known vulnerabilities found
```

---

---

#### Semgrep — broader SAST

```bash
python -m semgrep --config=p/python --config=p/fastapi llm_valet/
```

**What it checks:** OWASP Top 10 patterns, FastAPI-specific issues (unprotected routes, response model leaks), async pitfalls, and hundreds of Python security patterns that Bandit doesn't cover.

**Reading the output:**

```
llm_valet/api.py
  fastapi.security.missing-auth: Route /admin has no authentication dependency
  │ @app.get("/admin")
  ╰─────────────────── llm_valet/api.py:55

Found 1 finding in 1 file.
```

Format: `ruleset.rule-id: description` followed by the offending code and location.

- `p/python` rules cover general Python security patterns
- `p/fastapi` rules cover framework-specific issues

Each finding links to the rule documentation explaining the attack vector. Read it before deciding whether to fix or suppress.

To suppress a specific rule on a specific line:
```python
result = do_thing()  # nosemgrep: rule-id
```

Clean output:
```
Ran N rules on M files: 0 findings.
```

---

#### pytest with coverage

```bash
python -m pytest
```

Coverage is automatically enabled via `pyproject.toml` (`--cov=llm_valet --cov-fail-under=80`). The run fails if coverage drops below 80%.

**Reading the output:**

```
----------- coverage: platform linux, python 3.11 -----------
Name                              Stmts   Miss  Cover   Missing
---------------------------------------------------------------
llm_valet/api.py                    89      12    87%   45-52, 110
llm_valet/config.py                 48       3    94%   102-104
llm_valet/providers/ollama.py       97      18    81%   200-217
---------------------------------------------------------------
TOTAL                              234      33    86%
```

Columns:
- **Stmts** — total executable lines
- **Miss** — lines not executed by any test
- **Cover** — percentage covered
- **Missing** — line numbers with no test coverage

Lines in **Missing** are risk areas — untested code paths. For security-sensitive functions (auth, subprocess calls, config validation), these deserve tests before merging.

Run tests without failing on coverage threshold (for investigation):
```bash
python -m pytest --no-cov-on-fail --cov-fail-under=0
```

Run only unit tests:
```bash
python -m pytest tests/unit/
```

---

### Run everything at once

With the venv active:

```bash
python -m ruff check llm_valet svcmgr && \
python -m bandit -r llm_valet svcmgr -c pyproject.toml && \
python -m mypy llm_valet svcmgr && \
python -m semgrep --config=p/python --config=p/fastapi llm_valet/ && \
python -m pytest && \
python -m pip_audit
```

Or via hatch (manages its own env, no manual venv activation):

```bash
hatch run lint    # ruff + bandit + mypy + semgrep
hatch run test    # pytest with coverage
hatch run audit   # pip-audit
```

CI (`.github/workflows/ci.yml`) runs all tools on every push to `main` and `dev`:

| Job | Tools | Blocks merge? |
|---|---|---|
| Lint & Type Check | ruff, bandit, mypy | Yes |
| Tests & Coverage | pytest-cov (≥80%) | Yes |
| Semgrep SAST | p/python + p/fastapi | Yes |
| Dependency Audit | pip-audit | Yes |
| CodeQL | security-extended queries | Yes |

---

### Validating AI-generated security findings

When an AI tool (or another person) reports a vulnerability, apply this checklist before acting:

**1. Verify the file and line exist**

Open the cited file and go to the cited line. If the code isn't there, the finding is hallucinated.

**2. Check if a tool flags it**

Run Bandit and Ruff. If neither flags it, and it isn't a logic/type issue mypy would catch, the AI likely misidentified the risk. Real vulnerabilities in Python almost always have a corresponding Bandit rule.

**3. Understand the architecture before accepting a fix**

Read the Security section above and the threat model in `CLAUDE.md`. A finding that contradicts a documented design decision (e.g., "CORS is disabled" — it is, intentionally) means the AI doesn't understand the system.

**4. Common false-positive patterns to reject**

| AI claim | Why to reject |
|---|---|
| "No authentication enforcement" when auth exists | AI didn't recognise the framework's dependency injection pattern |
| "Hardcode a default secret" as a fix | Creates a shared-secret vulnerability far worse than the original |
| "Encrypt config with a hardcoded key" | Security theater — a fixed key provides no protection |
| "CORS disabled = vulnerability" | Empty CORS origins = same-origin only = secure default |
| "`:` in model name regex = path traversal" | `:` is Ollama's tag separator; model names go in JSON bodies, not file paths |
| Timeout values flagged as OWASP issues | Operational parameters, not security vulnerabilities |

**5. Trust tool output over AI narrative**

If Ruff, Bandit, and mypy are all clean, and the AI claims there is a critical vulnerability, ask the AI to cite the specific Bandit or CWE rule that applies. If it can't, the finding is likely wrong.

---

## License

MIT License — Copyright (c) 2026 [LegionForge](https://github.com/LegionForge) · jp@legionforge.org

Attribution required: all copies and distributions must include the above copyright notice per the MIT license terms.
