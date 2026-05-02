# llm-valet Wiki

llm-valet is a cross-platform utility that manages Ollama (and other LLM providers) lifecycle based on manual control or automatic resource/activity sensing.

---

## Pages

- [Architecture](Architecture) — Component overview, watchdog FSM, ThresholdEngine, security model, key design decisions
- [Module Reference](Module-Reference) — Public interfaces for every module: `api.py`, `watchdog.py`, `config.py`, `providers/`, `resources/`, `svcmgr/`

---

## Quick links

- [README](https://github.com/LegionForge/llm-valet#readme) — Install, configure, operate
- [SECURITY.md](https://github.com/LegionForge/llm-valet/blob/main/SECURITY.md) — Full threat model (T1–T8)
- [Roadmap](https://github.com/LegionForge/llm-valet/blob/main/docs/roadmap.md) — v0.6.0 gate + post-v1.0 backlog
- [API docs](http://localhost:8765/docs) — Auto-generated OpenAPI (requires running instance)

---

## Publishing this wiki

These files live in `docs/wiki/` in the main repo. To publish them to the GitHub wiki:

```bash
git clone https://github.com/LegionForge/llm-valet.wiki.git
cp docs/wiki/*.md llm-valet.wiki/
cd llm-valet.wiki
git add .
git commit -m "docs: architecture + module reference wiki pages"
git push
```
