"""
Session-level fixtures shared by all integration tests.

Requires a live Ollama instance at http://127.0.0.1:11434 with at least one
model installed. All tests are skipped automatically when Ollama is not
reachable or has no models available.

Run: pytest -m integration tests/integration/

NOTE — macOS launchd context: the Ollama launchd agent runs in a restricted
security context on macOS 15 and cannot access ~/.ollama. Run Ollama directly
from a terminal session before executing integration tests:
    brew services stop ollama
    ollama serve &
"""

from __future__ import annotations

import httpx
import pytest

OLLAMA_URL = "http://127.0.0.1:11434"

# Models excluded as test candidates.
# Cloud/API models have no local weights and can't be paused via keep_alive.
# Embedding models use /api/embed rather than /api/generate, so keep_alive=-1
# pre-warming via /api/generate does not work for them.
_EXCLUDED_MODEL_KEYWORDS = ("cloud", "embed")


@pytest.fixture(scope="session")
def ollama_url() -> str:
    """Skip the entire session when Ollama is not reachable at localhost:11434."""
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
        if r.status_code != 200:
            pytest.skip(f"Ollama not reachable at {OLLAMA_URL}")
    except httpx.HTTPError:
        pytest.skip(f"Ollama not reachable at {OLLAMA_URL}")
    return OLLAMA_URL


@pytest.fixture(scope="session")
def test_model(ollama_url: str) -> str:
    """
    Select the smallest locally installed model for integration tests.

    Prefers small models to minimise load/unload latency. Skips cloud-only
    models that have no local weights and cannot be evicted via keep_alive.
    Skips the entire session when no suitable model is installed.
    """
    r = httpx.get(f"{ollama_url}/api/tags", timeout=5.0)
    models = r.json().get("models", [])
    candidates = [
        m
        for m in models
        if not any(kw in m["name"].lower() for kw in _EXCLUDED_MODEL_KEYWORDS)
        and m.get("size", 0) > 0
    ]
    if not candidates:
        pytest.skip(
            "No suitable generative models installed — "
            "install at least one with `ollama pull` (e.g. qwen2.5:3b)"
        )
    # Pick the smallest model to keep load/unload fast in tests
    smallest = min(candidates, key=lambda m: m["size"])
    return str(smallest["name"])
