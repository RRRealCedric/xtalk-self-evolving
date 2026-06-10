from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..log_utils import logger
from ..serving.events import ASRResultFinal, MemoryRetrieved
from ..serving.interfaces import Manager
from .interfaces import MemoryStore
from .schema import MemorySearchResult
from .store import SQLiteMemoryStore
from .tools import build_memory_tools

if TYPE_CHECKING:
    from ..pipelines import Pipeline
    from ..serving.event_bus import EventBus


class MemoryManager(Manager):
    """Retrieve and expose long-term memories for an authenticated session."""

    def __init__(
        self,
        event_bus: "EventBus",
        session_id: str,
        pipeline: "Pipeline",
        config: dict[str, Any] | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.session_id = session_id
        self.pipeline = pipeline
        self.config = config or {}
        self._enabled = self._resolve_enabled()
        self._user_id = self._resolve_user_id()
        self._store = self._resolve_store()
        self._top_k = int(self._memory_config().get("top_k", 5))

        if self._enabled and self._user_id and self._store is not None:
            self._install_tools()

    @Manager.event_handler(ASRResultFinal, priority=80)
    async def _retrieve_for_turn(self, event: ASRResultFinal) -> None:
        """Retrieve relevant memories before turn-taking starts LLM generation."""

        if not self._enabled or not self._user_id or self._store is None:
            return
        text = (event.text or "").strip()
        if not text:
            await self._publish_retrieved([], query="")
            return
        try:
            results = await self._store.search(
                user_id=self._user_id,
                query=text,
                limit=max(1, min(10, self._top_k)),
            )
        except Exception as exc:
            logger.warning(
                "Memory retrieval failed - session: %s, error: %s",
                self.session_id,
                exc,
            )
            await self._publish_retrieved([], query=text)
            return

        payload = self._serialize_results(results)
        await self._publish_retrieved(payload, query=text)

    async def _publish_retrieved(
        self,
        memories: list[dict[str, Any]],
        *,
        query: str,
    ) -> None:
        """Publish retrieved memories and wait until agent context consumes them."""

        await self.event_bus.publish(
            MemoryRetrieved(
                session_id=self.session_id,
                memories=memories,
                query=query,
            ),
            wait_for_completion=True,
        )

    def _resolve_enabled(self) -> bool:
        memory_config = self._memory_config()
        value = memory_config.get("enabled", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    def _resolve_user_id(self) -> str | None:
        user_id = self.config.get("user_id")
        return str(user_id) if isinstance(user_id, str) and user_id else None

    def _resolve_store(self) -> MemoryStore | None:
        store = self.config.get("memory_store")
        if isinstance(store, MemoryStore):
            return store
        memory_config = self._memory_config()
        db_path = memory_config.get("sqlite_path")
        if not db_path:
            data_dir = self.config.get("data_dir") or "data"
            db_path = f"{data_dir}/memory/memory.sqlite3"
        try:
            return SQLiteMemoryStore(db_path)
        except Exception as exc:
            logger.warning("Failed to initialize memory store: %s", exc)
            return None

    def _memory_config(self) -> dict[str, Any]:
        value = self.config.get("memory")
        return value if isinstance(value, dict) else {}

    def _install_tools(self) -> None:
        agent = self.pipeline.get_agent()
        if agent is None or not hasattr(agent, "add_tools"):
            return
        try:
            agent.add_tools(
                build_memory_tools(
                    memory_store=self._store,
                    user_id_getter=lambda: self._user_id,
                    session_id_getter=lambda: self.session_id,
                )
            )
        except Exception as exc:
            logger.warning(
                "Failed to install memory tools - session: %s, error: %s",
                self.session_id,
                exc,
            )

    @staticmethod
    def _serialize_results(results: list[MemorySearchResult]) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for result in results:
            item = result.item
            payload.append(
                {
                    "id": item.id,
                    "type": item.type,
                    "content": item.content,
                    "source": item.source,
                    "confidence": item.confidence,
                    "score": result.score,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
            )
        return payload

    async def shutdown(self) -> None:
        return
