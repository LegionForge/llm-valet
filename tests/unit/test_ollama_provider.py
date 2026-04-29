"""
OllamaProvider HTTP interaction tests — all Ollama API calls are mocked.

Uses unittest.mock to intercept httpx.AsyncClient so no real network is
required.  Tests cover the most critical paths: status(), pause(), resume(),
load_model(), health_check(), and model name validation.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from llm_valet.providers.base import ProviderStatus
from llm_valet.providers.ollama import OllamaProvider

# ── Mock builders ─────────────────────────────────────────────────────────────


def _http_client(
    get_resp: MagicMock | None = None, post_resp: MagicMock | None = None
) -> MagicMock:
    """Return a context-manager-compatible mock for httpx.AsyncClient."""
    client = AsyncMock()
    if get_resp is not None:
        client.get.return_value = get_resp
    if post_resp is not None:
        client.post.return_value = post_resp
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _json_resp(data: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    resp.json.return_value = data
    return resp


def _ok_resp() -> MagicMock:
    return _json_resp({})


# ── health_check() ────────────────────────────────────────────────────────────


class TestHealthCheck:
    async def test_returns_true_when_api_tags_200(self) -> None:
        provider = OllamaProvider()
        client = _http_client(get_resp=_json_resp({"models": []}, status=200))
        with patch("httpx.AsyncClient", return_value=client):
            assert await provider.health_check() is True

    async def test_returns_false_on_http_error(self) -> None:
        import httpx

        provider = OllamaProvider()
        client = AsyncMock()
        client.get.side_effect = httpx.ConnectError("refused")
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient", return_value=client):
            assert await provider.health_check() is False


# ── status() ─────────────────────────────────────────────────────────────────


class TestStatus:
    async def test_returns_not_running_when_health_check_fails(self) -> None:
        provider = OllamaProvider()
        provider.health_check = AsyncMock(return_value=False)  # type: ignore[method-assign]
        result = await provider.status()
        assert result.running is False
        assert result.model_loaded is False
        assert result.model_name is None

    async def test_returns_running_no_model_when_api_ps_empty(self) -> None:
        provider = OllamaProvider()
        provider.health_check = AsyncMock(return_value=True)  # type: ignore[method-assign]
        client = _http_client(get_resp=_json_resp({"models": []}))
        with patch("httpx.AsyncClient", return_value=client):
            result = await provider.status()
        assert result.running is True
        assert result.model_loaded is False
        assert result.model_name is None

    async def test_returns_model_fields_from_api_ps(self) -> None:
        provider = OllamaProvider()
        provider.health_check = AsyncMock(return_value=True)  # type: ignore[method-assign]
        api_ps_data = {
            "models": [
                {
                    "name": "llama3:latest",
                    "size": 4_000_000_000,  # 4 GB in bytes
                    "size_vram": 3_000_000_000,  # 3 GB GPU portion
                    "context_length": 8192,
                }
            ]
        }
        client = _http_client(get_resp=_json_resp(api_ps_data))
        with patch("httpx.AsyncClient", return_value=client):
            result = await provider.status()
        assert result.running is True
        assert result.model_loaded is True
        assert result.model_name == "llama3:latest"
        assert result.memory_used_mb == 3814  # 4_000_000_000 // (1024*1024)
        assert result.size_vram_mb == 2861  # 3_000_000_000 // (1024*1024)
        assert result.loaded_context_length == 8192

    async def test_size_vram_zero_returns_none(self) -> None:
        """size_vram=0 (pure CPU model) → size_vram_mb=None (or 0)."""
        provider = OllamaProvider()
        provider.health_check = AsyncMock(return_value=True)  # type: ignore[method-assign]
        api_ps_data = {
            "models": [
                {
                    "name": "llama3:latest",
                    "size": 2_000_000_000,
                    "size_vram": 0,
                    "context_length": 4096,
                }
            ]
        }
        client = _http_client(get_resp=_json_resp(api_ps_data))
        with patch("httpx.AsyncClient", return_value=client):
            result = await provider.status()
        # size_vram=0 → size_vram_mb=None (due to `or None` in implementation)
        assert result.size_vram_mb is None
        assert result.memory_used_mb is not None


# ── pause() ───────────────────────────────────────────────────────────────────


class TestPause:
    async def test_pause_sends_keep_alive_zero(self) -> None:
        provider = OllamaProvider(model_name="llama3")
        captured: dict = {}

        async def capture_post(url: str, json: dict | None = None) -> MagicMock:
            captured.update(json or {})
            return _json_resp({"done_reason": "unload"})

        client = AsyncMock()
        client.post.side_effect = capture_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        # Mock status() to return a loaded model so _last_loaded_ctx is set
        provider.status = AsyncMock(
            return_value=ProviderStatus(  # type: ignore[method-assign]
                running=True,
                model_loaded=True,
                model_name="llama3",
                memory_used_mb=4096,
                loaded_context_length=None,
            )
        )

        with patch("httpx.AsyncClient", return_value=client):
            result = await provider.pause()

        assert result is True
        assert captured["keep_alive"] == 0
        assert captured["model"] == "llama3"

    async def test_pause_caches_model_name_on_success(self) -> None:
        provider = OllamaProvider(model_name="llama3")
        provider.status = AsyncMock(
            return_value=ProviderStatus(  # type: ignore[method-assign]
                running=True,
                model_loaded=True,
                model_name="llama3",
                memory_used_mb=4096,
            )
        )
        client = _http_client(post_resp=_json_resp({"done_reason": "unload"}))
        with patch("httpx.AsyncClient", return_value=client):
            await provider.pause()
        assert provider._last_loaded_model == "llama3"

    async def test_pause_returns_false_on_unexpected_done_reason(self) -> None:
        provider = OllamaProvider(model_name="llama3")
        provider.status = AsyncMock(
            return_value=ProviderStatus(  # type: ignore[method-assign]
                running=True,
                model_loaded=True,
                model_name="llama3",
                memory_used_mb=4096,
            )
        )
        client = _http_client(post_resp=_json_resp({"done_reason": "stop"}))
        with patch("httpx.AsyncClient", return_value=client):
            result = await provider.pause()
        assert result is False

    async def test_pause_skips_when_no_model(self) -> None:
        """No model loaded → pause is a no-op that returns True."""
        provider = OllamaProvider()
        provider.status = AsyncMock(
            return_value=ProviderStatus(  # type: ignore[method-assign]
                running=True,
                model_loaded=False,
                model_name=None,
                memory_used_mb=None,
            )
        )
        # _resolve_model() will find nothing and return None
        result = await provider.pause()
        assert result is True


# ── resume() ─────────────────────────────────────────────────────────────────


class TestResume:
    async def test_resume_sends_keep_alive_minus_one_and_stream_false(self) -> None:
        provider = OllamaProvider(model_name="llama3")
        provider._last_loaded_ctx = None
        captured: dict = {}

        async def capture_post(url: str, json: dict | None = None) -> MagicMock:
            captured.update(json or {})
            return _ok_resp()

        client = AsyncMock()
        client.post.side_effect = capture_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=client):
            with patch.object(provider, "_resolve_model", new=AsyncMock(return_value="llama3")):
                result = await provider.resume()

        assert result is True
        assert captured["keep_alive"] == -1
        assert captured["stream"] is False
        assert "options" not in captured

    async def test_resume_includes_num_ctx_from_last_loaded_ctx(self) -> None:
        provider = OllamaProvider(model_name="llama3")
        provider._last_loaded_ctx = 32768
        captured: dict = {}

        async def capture_post(url: str, json: dict | None = None) -> MagicMock:
            captured.update(json or {})
            return _ok_resp()

        client = AsyncMock()
        client.post.side_effect = capture_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=client):
            with patch.object(provider, "_resolve_model", new=AsyncMock(return_value="llama3")):
                result = await provider.resume()

        assert result is True
        assert captured.get("options") == {"num_ctx": 32768}

    async def test_resume_returns_false_when_no_model_resolved(self) -> None:
        provider = OllamaProvider()
        with patch.object(provider, "_resolve_model", new=AsyncMock(return_value=None)):
            result = await provider.resume()
        assert result is False


# ── load_model() ──────────────────────────────────────────────────────────────


class TestLoadModel:
    async def test_load_model_sends_correct_payload_no_ctx(self) -> None:
        provider = OllamaProvider()
        captured: dict = {}

        async def capture_post(url: str, json: dict | None = None) -> MagicMock:
            captured.update(json or {})
            return _ok_resp()

        client = AsyncMock()
        client.post.side_effect = capture_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        # No current model loaded
        with patch.object(provider, "_resolve_model", new=AsyncMock(return_value=None)):
            with patch("httpx.AsyncClient", return_value=client):
                result = await provider.load_model("llama3:latest")

        assert result is True
        assert captured["model"] == "llama3:latest"
        assert captured["keep_alive"] == -1
        assert captured["stream"] is False
        assert "options" not in captured

    async def test_load_model_sends_num_ctx_when_provided(self) -> None:
        provider = OllamaProvider()
        captured: dict = {}

        async def capture_post(url: str, json: dict | None = None) -> MagicMock:
            captured.update(json or {})
            return _ok_resp()

        client = AsyncMock()
        client.post.side_effect = capture_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch.object(provider, "_resolve_model", new=AsyncMock(return_value=None)):
            with patch("httpx.AsyncClient", return_value=client):
                result = await provider.load_model("llama3:latest", num_ctx=8192)

        assert result is True
        assert captured.get("options") == {"num_ctx": 8192}

    async def test_load_model_ignores_num_ctx_below_512(self) -> None:
        """num_ctx < 512 is silently ignored per spec."""
        provider = OllamaProvider()
        captured: dict = {}

        async def capture_post(url: str, json: dict | None = None) -> MagicMock:
            captured.update(json or {})
            return _ok_resp()

        client = AsyncMock()
        client.post.side_effect = capture_post
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        with patch.object(provider, "_resolve_model", new=AsyncMock(return_value=None)):
            with patch("httpx.AsyncClient", return_value=client):
                result = await provider.load_model("llama3:latest", num_ctx=256)

        assert result is True
        assert "options" not in captured

    async def test_load_model_rejects_invalid_name(self) -> None:
        provider = OllamaProvider()
        result = await provider.load_model("bad name; rm -rf /")
        assert result is False

    async def test_load_model_updates_model_name_on_success(self) -> None:
        provider = OllamaProvider()
        client = _http_client(post_resp=_ok_resp())
        with patch.object(provider, "_resolve_model", new=AsyncMock(return_value=None)):
            with patch("httpx.AsyncClient", return_value=client):
                await provider.load_model("qwen3:0.6b")
        assert provider._model_name == "qwen3:0.6b"
        assert provider._last_loaded_model == "qwen3:0.6b"
