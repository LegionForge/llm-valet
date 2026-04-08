from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ProviderStatus:
    running: bool
    model_loaded: bool
    model_name: str | None
    memory_used_mb: int | None


@dataclass
class ModelInfo:
    name: str
    size_mb: int


class LLMProvider(ABC):
    @abstractmethod
    async def start(self) -> bool: ...

    @abstractmethod
    async def stop(self) -> bool: ...

    @abstractmethod
    async def pause(self) -> bool: ...

    @abstractmethod
    async def resume(self) -> bool: ...

    @abstractmethod
    async def status(self) -> ProviderStatus: ...

    @abstractmethod
    async def health_check(self) -> bool: ...

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...

    @abstractmethod
    async def load_model(self, model_name: str) -> bool: ...
