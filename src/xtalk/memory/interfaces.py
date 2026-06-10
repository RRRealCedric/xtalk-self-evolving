from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .schema import MemoryItem, MemorySearchResult, MemoryType


class MemoryStore(ABC):
    """Abstract long-term memory storage interface."""

    @abstractmethod
    async def add(self, item: MemoryItem) -> str:
        """Store one memory item and return its identifier."""

    @abstractmethod
    async def search(
        self,
        *,
        user_id: str,
        query: str,
        limit: int = 5,
        memory_types: list[MemoryType] | None = None,
    ) -> list[MemorySearchResult]:
        """Search memories owned by ``user_id``."""

    @abstractmethod
    async def list(
        self,
        *,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
        memory_types: list[MemoryType] | None = None,
    ) -> list[MemoryItem]:
        """List memories owned by ``user_id``."""

    @abstractmethod
    async def update(
        self, *, user_id: str, memory_id: str, patch: dict[str, Any]
    ) -> bool:
        """Patch a memory item. Return ``True`` when an item changed."""

    @abstractmethod
    async def delete(self, *, user_id: str, memory_id: str) -> bool:
        """Soft-delete a memory item owned by ``user_id``."""
