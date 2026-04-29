# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | ✅ Current |
| 0.1.x   | ✅ Receives security fixes |
| < 0.1   | ❌ No longer supported |

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report vulnerabilities privately via GitHub's
[Security Advisories](https://github.com/LegionForge/llm-valet/security/advisories/new)
feature, or email **jp@legionforge.org** with subject line `[llm-valet] Security`.

Include:
- Description of the vulnerability
- Steps to reproduce
- Affected versions
- Potential impact

We aim to acknowledge reports within **48 hours** and provide a fix or mitigation
plan within **14 days** for confirmed issues.

## Threat Model Summary

llm-valet manages a local LLM service and listens on a network port. The key threats
and mitigations are documented in [`CLAUDE.md`](CLAUDE.md) (T1–T8). In brief:

| Threat | Mitigation |
|--------|-----------|
| Unauthenticated service control | `X-API-Key` required for all non-localhost access |
| DNS rebinding | `TrustedHostMiddleware` allowlist |
| CORS wildcard | `allow_origins` is config-only, never `"*"` |
| Command injection via model names | `^[a-zA-Z0-9:._-]+$` validation; `shell=False` everywhere |
| SSRF via provider URL | `ollama_url` validated to localhost/RFC1918 on load |
| XSS via WebUI | `textContent` only — no `innerHTML` for API-sourced data |
| Privilege escalation | User-level services only; startup exits if run as root |
| Config file exposure | `chmod 0600` enforced on every write to `config.yaml` |

## Security Defaults

- Binds to `127.0.0.1` by default — LAN exposure requires explicit `host: 0.0.0.0` in config
- No API key required for localhost; key required for all other origins
- WebUI displays a visible badge when LAN-exposed without auth

## Scope

In scope: the llm-valet service, API endpoints, WebUI, config handling, subprocess calls.
Out of scope: the Ollama service itself, OS-level vulnerabilities, issues in dependencies
(report those upstream).
