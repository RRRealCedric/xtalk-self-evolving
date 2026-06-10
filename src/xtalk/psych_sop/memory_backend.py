"""Lightweight single-user memory backends for the psychology demo."""

from __future__ import annotations

import json
import os
import shutil
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


DEBUG_USER_ID = "xtalk_psych_demo_user"
DEFAULT_MEMORY_PATH = Path("data/psych_sop_demo/memory.json")


class PsychMemoryBackend(ABC):
    """Memory interface for the psychology demo.

    TODO(memory): switch from single-user debug memory to proper user/session anchor.
    """

    @abstractmethod
    def search(
        self,
        query: str,
        scope: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def add_dialogue_turn(
        self,
        user_text: str,
        assistant_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        pass

    @abstractmethod
    def add_note(
        self,
        content: str,
        scope: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        pass

    @abstractmethod
    def reset(self) -> None:
        pass

    @abstractmethod
    def export(self, path: str | Path) -> None:
        pass


class LocalJsonMemoryBackend(PsychMemoryBackend):
    """Small JSON-file memory store for one debug user."""

    def __init__(
        self,
        path: str | Path = DEFAULT_MEMORY_PATH,
        user_id: str = DEBUG_USER_ID,
    ) -> None:
        self.path = Path(path)
        self.user_id = user_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])

    def search(
        self,
        query: str,
        scope: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        query_terms = {term for term in query.lower().split() if term}
        records = [
            record
            for record in self._read()
            if record.get("user_id") == self.user_id
            and (scope is None or record.get("scope") == scope)
        ]
        scored: list[tuple[int, dict[str, Any]]] = []
        for record in records:
            content = str(record.get("content", "")).lower()
            score = 1 if query and query.lower() in content else 0
            score += sum(1 for term in query_terms if term in content)
            if scope and record.get("scope") == scope:
                score += 1
            scored.append((score, record))
        scored.sort(
            key=lambda item: (item[0], item[1].get("timestamp", "")), reverse=True
        )
        return [
            record for score, record in scored[:top_k] if score > 0 or not query_terms
        ]

    def add_dialogue_turn(
        self,
        user_text: str,
        assistant_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.add_note(
            f"用户：{user_text}\n助手：{assistant_text}",
            scope="dialogue_memory",
            metadata=metadata,
        )

    def add_note(
        self,
        content: str,
        scope: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        records = self._read()
        now = datetime.utcnow().isoformat()
        meta = dict(metadata or {})
        meta.setdefault("scope", scope)
        meta.setdefault("timestamp", now)
        records.append(
            {
                "id": str(uuid4()),
                "user_id": self.user_id,
                "content": content,
                "scope": scope,
                "metadata": meta,
                "timestamp": now,
            }
        )
        self._write(records)

    def reset(self) -> None:
        self._write([])

    def export(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.path, target)

    def _read(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class Mem0MemoryBackend(PsychMemoryBackend):
    """Optional Mem0 adapter.

    This wrapper is deliberately tiny. It is selected only when mem0 is
    installed and a relevant API key is present.
    """

    def __init__(self, user_id: str = DEBUG_USER_ID) -> None:
        try:
            from mem0 import Memory
        except ImportError as exc:
            raise RuntimeError("mem0 is not installed") from exc
        self.user_id = user_id
        self.memory = Memory.from_config({})

    def search(
        self,
        query: str,
        scope: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        filters = {"scope": scope} if scope else None
        result = self.memory.search(
            query=query,
            user_id=self.user_id,
            limit=top_k,
            filters=filters,
        )
        return result if isinstance(result, list) else []

    def add_dialogue_turn(
        self,
        user_text: str,
        assistant_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.add_note(
            f"用户：{user_text}\n助手：{assistant_text}",
            scope="dialogue_memory",
            metadata=metadata,
        )

    def add_note(
        self,
        content: str,
        scope: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        meta = dict(metadata or {})
        meta["scope"] = scope
        self.memory.add(content, user_id=self.user_id, metadata=meta)

    def reset(self) -> None:
        self.memory.delete_all(user_id=self.user_id)

    def export(self, path: str | Path) -> None:
        records = self.search("", top_k=100)
        Path(path).write_text(json.dumps(records, ensure_ascii=False, indent=2))


def create_memory_backend(
    *,
    prefer_mem0: bool = True,
    path: str | Path = DEFAULT_MEMORY_PATH,
    user_id: str = DEBUG_USER_ID,
) -> PsychMemoryBackend:
    """Create Mem0 backend when available, otherwise local JSON fallback."""

    if prefer_mem0 and os.getenv("MEM0_API_KEY"):
        try:
            return Mem0MemoryBackend(user_id=user_id)
        except Exception:
            pass
    return LocalJsonMemoryBackend(path=path, user_id=user_id)
