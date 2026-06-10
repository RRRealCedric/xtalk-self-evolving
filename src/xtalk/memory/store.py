from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .interfaces import MemoryStore
from .schema import MemoryItem, MemorySearchResult, MemoryType, utc_now_iso


_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text or "")}


def _row_to_item(row: sqlite3.Row) -> MemoryItem:
    metadata: dict[str, Any] = {}
    raw_metadata = row["metadata_json"]
    if raw_metadata:
        try:
            decoded = json.loads(str(raw_metadata))
            if isinstance(decoded, dict):
                metadata = decoded
        except json.JSONDecodeError:
            metadata = {}
    return MemoryItem(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        session_id=row["session_id"],
        type=str(row["type"]),
        content=str(row["content"]),
        source=str(row["source"] or "explicit"),
        confidence=float(row["confidence"] or 0.0),
        metadata=metadata,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class SQLiteMemoryStore(MemoryStore):
    """SQLite-backed long-term memory store.

    The MVP uses deterministic keyword scoring so it has no external vector DB
    dependency. The schema intentionally keeps enough metadata to add embeddings
    or Chroma later without changing the manager/API contract.
    """

    VALID_TYPES = {"semantic", "episodic", "procedural", "reflective"}

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'explicit',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    deleted_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_memories_user_active
                    ON memories(user_id, deleted_at, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_memories_user_type
                    ON memories(user_id, type, deleted_at);
                """
            )

    async def add(self, item: MemoryItem) -> str:
        return await asyncio.to_thread(self._add_sync, item)

    def _add_sync(self, item: MemoryItem) -> str:
        memory_id = item.id or str(uuid.uuid4())
        memory_type = item.type if item.type in self.VALID_TYPES else "semantic"
        now = utc_now_iso()
        metadata_json = json.dumps(item.metadata or {}, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memories (
                    id, user_id, session_id, type, content, source,
                    confidence, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    item.user_id,
                    item.session_id,
                    memory_type,
                    item.content.strip(),
                    item.source or "explicit",
                    max(0.0, min(1.0, float(item.confidence))),
                    metadata_json,
                    item.created_at or now,
                    item.updated_at or now,
                ),
            )
        return memory_id

    async def search(
        self,
        *,
        user_id: str,
        query: str,
        limit: int = 5,
        memory_types: list[MemoryType] | None = None,
    ) -> list[MemorySearchResult]:
        return await asyncio.to_thread(
            self._search_sync,
            user_id=user_id,
            query=query,
            limit=limit,
            memory_types=memory_types,
        )

    def _search_sync(
        self,
        *,
        user_id: str,
        query: str,
        limit: int,
        memory_types: list[MemoryType] | None,
    ) -> list[MemorySearchResult]:
        limit = max(1, min(50, int(limit or 5)))
        candidates = self._list_sync(
            user_id=user_id,
            limit=200,
            offset=0,
            memory_types=memory_types,
        )
        query_tokens = _tokens(query)
        query_text = " ".join((query or "").split()).lower()
        scored: list[MemorySearchResult] = []
        for item in candidates:
            content = item.content.lower()
            content_tokens = _tokens(item.content)
            overlap = len(query_tokens & content_tokens)
            score = float(overlap)
            if query_text and query_text in content:
                score += 5.0
            if not query_tokens and not query_text:
                score = 0.1
            if score <= 0.0:
                continue
            scored.append(MemorySearchResult(item=item, score=score))
        scored.sort(
            key=lambda result: (result.score, result.item.updated_at), reverse=True
        )
        selected = scored[:limit]
        if selected:
            self._touch_sync(
                user_id=user_id, memory_ids=[result.item.id for result in selected]
            )
        return selected

    async def list(
        self,
        *,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
        memory_types: list[MemoryType] | None = None,
    ) -> list[MemoryItem]:
        return await asyncio.to_thread(
            self._list_sync,
            user_id=user_id,
            limit=limit,
            offset=offset,
            memory_types=memory_types,
        )

    def _list_sync(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
        memory_types: list[MemoryType] | None,
    ) -> list[MemoryItem]:
        limit = max(1, min(200, int(limit or 50)))
        offset = max(0, int(offset or 0))
        params: list[Any] = [user_id]
        type_clause = ""
        if memory_types:
            clean_types = [item for item in memory_types if item in self.VALID_TYPES]
            if clean_types:
                placeholders = ", ".join("?" for _ in clean_types)
                type_clause = f"AND type IN ({placeholders})"
                params.extend(clean_types)
        params.extend([limit, offset])
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, user_id, session_id, type, content, source, confidence,
                       metadata_json, created_at, updated_at
                FROM memories
                WHERE user_id = ? AND deleted_at IS NULL
                {type_clause}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [_row_to_item(row) for row in rows]

    async def update(
        self, *, user_id: str, memory_id: str, patch: dict[str, Any]
    ) -> bool:
        return await asyncio.to_thread(
            self._update_sync,
            user_id=user_id,
            memory_id=memory_id,
            patch=patch,
        )

    def _update_sync(
        self, *, user_id: str, memory_id: str, patch: dict[str, Any]
    ) -> bool:
        allowed: dict[str, Any] = {}
        if "content" in patch:
            allowed["content"] = str(patch["content"]).strip()
        if "type" in patch and patch["type"] in self.VALID_TYPES:
            allowed["type"] = patch["type"]
        if "confidence" in patch:
            allowed["confidence"] = max(0.0, min(1.0, float(patch["confidence"])))
        if "metadata" in patch and isinstance(patch["metadata"], dict):
            allowed["metadata_json"] = json.dumps(patch["metadata"], ensure_ascii=False)
        if not allowed:
            return False
        allowed["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in allowed)
        params = list(allowed.values()) + [user_id, memory_id]
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE memories
                SET {assignments}
                WHERE user_id = ? AND id = ? AND deleted_at IS NULL
                """,
                params,
            )
        return cur.rowcount > 0

    async def delete(self, *, user_id: str, memory_id: str) -> bool:
        return await asyncio.to_thread(
            self._delete_sync,
            user_id=user_id,
            memory_id=memory_id,
        )

    def _delete_sync(self, *, user_id: str, memory_id: str) -> bool:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE memories
                SET deleted_at = ?, updated_at = ?
                WHERE user_id = ? AND id = ? AND deleted_at IS NULL
                """,
                (now, now, user_id, memory_id),
            )
        return cur.rowcount > 0

    def _touch_sync(self, *, user_id: str, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        placeholders = ", ".join("?" for _ in memory_ids)
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                f"""
                UPDATE memories
                SET last_accessed_at = ?
                WHERE user_id = ? AND id IN ({placeholders}) AND deleted_at IS NULL
                """,
                [now, user_id, *memory_ids],
            )
