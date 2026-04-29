"""Tests for OllamaProvider context window preservation across PAUSE→RESUME.

The bug: pause() evicts the model, clearing /api/ps. resume() then calls
/api/generate with keep_alive=-1 but no options, so Ollama reloads the model
at its own default context length, discarding any num_ctx the user had set.

The fix: pause() reads context_length from /api/ps before eviction and caches
it in _last_loaded_ctx. resume() re-applies it via options.num_ctx.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from llm_valet.providers.base import ProviderStatus
from llm_valet.providers.ollama import OllamaProvider


def _mock_http_client(post_return_value: MagicMock) -> MagicMock:
    """Return a context-manager-compatible mock for httpx.AsyncClient."""
    client = AsyncMock()
    client.post.return_value = post_return_value
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _ok_pause_response() -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"done_reason": "unload"}
    return resp


def _ok_resume_response() -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    return resp


# ── pause() captures context_length ───────────────────────────────────────────


class TestPauseCapturesContext:
    async def test_captures_loaded_context_length(self) -> None:
        provider = OllamaProvider(model_name="llama3")
        provider.status = AsyncMock(
            return_value=ProviderStatus(  # type: ignore[method-assign]
                running=True,
                model_loaded=True,
                model_name="llama3",
                memory_used_mb=4096,
                loaded_context_length=8192,
            )
        )
        with patch("httpx.AsyncClient", return_value=_mock_http_client(_ok_pause_response())):
            await provider.pause()

        assert provider._last_loaded_ctx == 8192

    async def test_overwrites_stale_ctx_with_none_when_not_reported(self) -> None:
        """If /api/ps stops reporting context_length, the cached value must be cleared."""
        provider = OllamaProvider(model_name="llama3")
        provider._last_loaded_ctx = 4096  # stale value from a prior pause
        provider.status = AsyncMock(
            return_value=ProviderStatus(  # type: ignore[method-assign]
                running=True,
                model_loaded=True,
                model_name="llama3",
                memory_used_mb=4096,
                loaded_context_length=None,
            )
        )
        with patch("httpx.AsyncClient", return_value=_mock_http_client(_ok_pause_response())):
            await provider.pause()

        assert provider._last_loaded_ctx is None

    async def test_captures_none_when_no_model_loaded(self) -> None:
        provider = OllamaProvider(model_name="llama3")
        provider._last_loaded_ctx = 8192  # stale
        provider.status = AsyncMock(
            return_value=ProviderStatus(  # type: ignore[method-assign]
                running=True,
                model_loaded=False,
                model_name=None,
                memory_used_mb=None,
            )
        )
        # pause() will skip (no model) after the status() call
        await provider.pause()

        assert provider._last_loaded_ctx is None


# ── resume() restores context_length ──────────────────────────────────────────


class TestResumeRestoresContext:
    async def test_passes_num_ctx_when_captured(self) -> None:
        provider = OllamaProvider(model_name="llama3")
        provider._last_loaded_ctx = 8192

        captured: dict[str, object] = {}

        async def capture_post(url: str, json: dict[str, object] | None = None) -> MagicMock:
            captured.update(json or {})
            return _ok_resume_response()

        client = AsyncMock()
        client.post.side_effect = capture_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=client):
            with patch.object(provider, "_resolve_model", new=AsyncMock(return_value="llama3")):
                result = await provider.resume()

        assert result is True
        assert captured.get("options") == {"num_ctx": 8192}

    async def test_omits_options_when_ctx_is_none(self) -> None:
        provider = OllamaProvider(model_name="llama3")
        provider._last_loaded_ctx = None

        captured: dict[str, object] = {}

        async def capture_post(url: str, json: dict[str, object] | None = None) -> MagicMock:
            captured.update(json or {})
            return _ok_resume_response()

        client = AsyncMock()
        client.post.side_effect = capture_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=client):
            with patch.object(provider, "_resolve_model", new=AsyncMock(return_value="llama3")):
                result = await provider.resume()

        assert result is True
        assert "options" not in captured

    async def test_preserves_keep_alive_and_stream_false(self) -> None:
        """Core resume payload fields must not be dropped when options are added."""
        provider = OllamaProvider(model_name="llama3")
        provider._last_loaded_ctx = 4096

        captured: dict[str, object] = {}

        async def capture_post(url: str, json: dict[str, object] | None = None) -> MagicMock:
            captured.update(json or {})
            return _ok_resume_response()

        client = AsyncMock()
        client.post.side_effect = capture_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=client):
            with patch.object(provider, "_resolve_model", new=AsyncMock(return_value="llama3")):
                await provider.resume()

        assert captured["keep_alive"] == -1
        assert captured["stream"] is False
