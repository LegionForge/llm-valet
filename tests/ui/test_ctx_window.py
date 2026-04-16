"""
WebUI tests — context window selector rendering.

These tests load index.html in a headless Chromium browser, mock the backend
API, and verify that the context window dropdown shows correct labels for
models with various context lengths.

Requires: pip install playwright pytest-playwright && playwright install chromium
Run:      pytest tests/ui/ -m ui -v
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.ui.conftest import make_api_mock


pytestmark = pytest.mark.ui


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_with_model(page: Page, base_url: str, model_name: str, context_length: int) -> None:
    """Navigate to the WebUI with a single model available and select it."""
    models = [{"name": model_name, "size_mb": 988, "context_length": context_length}]
    make_api_mock(page, models)
    page.goto(f"{base_url}/index.html")
    # <option> elements inside <select> are never "visible" — use state="attached"
    page.wait_for_selector(f'#model-select option[value="{model_name}"]', state="attached", timeout=5000)
    page.select_option("#model-select", model_name)
    # onModelSelectChange() is synchronous — ctx row updates immediately on select
    # Wait for the row to have display:flex (set by JS when maxCtx is known)
    page.wait_for_function("document.getElementById('ctx-select-row').style.display === 'flex'", timeout=3000)


def _ctx_option_texts(page: Page) -> list[str]:
    """Return all non-empty ctx-select option text values."""
    return [
        t.strip()
        for t in page.locator("#ctx-select option").all_text_contents()
        if t.strip()
    ]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestCtxLabelRendering:
    """Verify _fmtCtxLabel produces correct labels for all supported context sizes."""

    def test_256k_model_shows_256k_label(self, page: Page, static_server: str) -> None:
        """
        Regression test for B1 (v0.5.2): a model with context_length=262144
        was mislabeled as '128K tokens (model max)' instead of '256K tokens (model max)'.
        """
        _load_with_model(page, static_server, "qwen3.5:0.8b", 262144)
        options = _ctx_option_texts(page)

        # The model-max entry must say 256K, not 128K
        assert any("256K tokens (model max)" in o for o in options), (
            f"Expected '256K tokens (model max)' in ctx options, got: {options}"
        )
        assert not any(o == "128K tokens (model max)" for o in options), (
            f"'128K tokens (model max)' should not appear for a 256K model — got: {options}"
        )

    def test_256k_model_includes_smaller_options(self, page: Page, static_server: str) -> None:
        """All standard sizes below 256K should also appear as options."""
        _load_with_model(page, static_server, "qwen3.5:0.8b", 262144)
        options = _ctx_option_texts(page)

        for expected in ["4K tokens", "8K tokens", "16K tokens", "32K tokens",
                         "64K tokens", "128K tokens"]:
            assert any(expected in o for o in options), (
                f"Expected '{expected}' in options for 256K model, got: {options}"
            )

    def test_128k_model_labeled_correctly(self, page: Page, static_server: str) -> None:
        """A model with exactly 131072 context should show '128K tokens (model max)'."""
        _load_with_model(page, static_server, "llama3.2:3b", 131072)
        options = _ctx_option_texts(page)

        assert any("128K tokens (model max)" in o for o in options), (
            f"Expected '128K tokens (model max)' for 131072 ctx model, got: {options}"
        )
        # Should NOT have a 256K option — model doesn't support it
        assert not any("256K" in o for o in options), (
            f"256K should not appear for a 128K model, got: {options}"
        )

    def test_1m_model_shows_1m_label(self, page: Page, static_server: str) -> None:
        """A model with 1M RoPE ceiling should show '1M tokens (model max)'."""
        _load_with_model(page, static_server, "mistral-nemo:12b", 1048576)
        options = _ctx_option_texts(page)

        assert any("1M tokens (model max)" in o for o in options), (
            f"Expected '1M tokens (model max)' for 1M ctx model, got: {options}"
        )

    def test_512k_model_shows_512k_label(self, page: Page, static_server: str) -> None:
        """A model with 524288 context should show '512K tokens (model max)'."""
        _load_with_model(page, static_server, "qwen3.5:7b", 524288)
        options = _ctx_option_texts(page)

        assert any("512K tokens (model max)" in o for o in options), (
            f"Expected '512K tokens (model max)' for 512K ctx model, got: {options}"
        )

    def test_4k_model_shows_no_larger_options(self, page: Page, static_server: str) -> None:
        """A 4K model should not offer 8K, 16K, etc."""
        _load_with_model(page, static_server, "phi3:mini", 4096)
        options = _ctx_option_texts(page)

        for unexpected in ["8K tokens", "16K tokens", "32K tokens", "64K tokens", "128K tokens"]:
            assert not any(unexpected in o for o in options), (
                f"'{unexpected}' should not appear for a 4K model, got: {options}"
            )
        assert any("4K tokens (model max)" in o for o in options), (
            f"Expected '4K tokens (model max)', got: {options}"
        )


class TestCtxRowVisibility:
    """Verify the context window row appears/disappears correctly."""

    def test_ctx_row_hidden_when_no_model_selected(
        self, page: Page, static_server: str
    ) -> None:
        """The ctx selector row should be hidden until a model is selected."""
        make_api_mock(page, [{"name": "qwen3.5:0.8b", "size_mb": 988, "context_length": 262144}])
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector("#model-select", timeout=5000)

        # Ensure no model is selected
        page.select_option("#model-select", "")
        # Row should not be visible — check computed style since initial hidden state
        # is set by CSS (.ctx-select-row { display: none }) not an inline style.
        displayed = page.evaluate("getComputedStyle(document.getElementById('ctx-select-row')).display")
        assert displayed == "none", f"ctx-select-row should be hidden with no model, got display={displayed!r}"

    def test_ctx_row_visible_after_model_selected(
        self, page: Page, static_server: str
    ) -> None:
        """Selecting a model with known context_length should show the ctx row."""
        _load_with_model(page, static_server, "qwen3.5:0.8b", 262144)
        row = page.locator("#ctx-select-row")
        expect(row).to_be_visible()

    def test_ctx_row_hidden_for_model_without_context_length(
        self, page: Page, static_server: str
    ) -> None:
        """If the API returns context_length: null, the row should stay hidden."""
        models = [{"name": "unknown-model:latest", "size_mb": 500, "context_length": None}]
        make_api_mock(page, models)
        page.goto(f"{static_server}/index.html")
        page.wait_for_selector('#model-select option[value="unknown-model:latest"]', state="attached", timeout=5000)
        page.select_option("#model-select", "unknown-model:latest")

        row = page.locator("#ctx-select-row")
        expect(row).to_have_css("display", "none")
