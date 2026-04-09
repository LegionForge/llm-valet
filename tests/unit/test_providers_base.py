"""Tests for providers/base.py dataclasses and ABC contract."""
from llm_valet.providers.base import ModelInfo, ProviderStatus


def test_provider_status_fields() -> None:
    s = ProviderStatus(running=True, model_loaded=True, model_name="llama3", memory_used_mb=4096)
    assert s.running is True
    assert s.model_loaded is True
    assert s.model_name == "llama3"
    assert s.memory_used_mb == 4096


def test_provider_status_optional_fields() -> None:
    s = ProviderStatus(running=False, model_loaded=False, model_name=None, memory_used_mb=None)
    assert s.model_name is None
    assert s.memory_used_mb is None


def test_provider_status_size_vram_mb_defaults_none() -> None:
    """size_vram_mb is optional — absent from providers that don't expose VRAM split."""
    s = ProviderStatus(running=True, model_loaded=True, model_name="llama3", memory_used_mb=4096)
    assert s.size_vram_mb is None


def test_provider_status_size_vram_mb_present() -> None:
    """When Ollama /api/ps returns size_vram, the field is populated."""
    s = ProviderStatus(
        running=True,
        model_loaded=True,
        model_name="llama3",
        memory_used_mb=6144,
        size_vram_mb=4096,
    )
    assert s.size_vram_mb == 4096
    assert s.memory_used_mb == 6144


def test_model_info_fields() -> None:
    m = ModelInfo(name="qwen3.5:latest", size_mb=8141, context_length=32768)
    assert m.name == "qwen3.5:latest"
    assert m.size_mb == 8141
    assert m.context_length == 32768


def test_model_info_no_context() -> None:
    m = ModelInfo(name="llama3.2:1b", size_mb=1200, context_length=None)
    assert m.context_length is None
