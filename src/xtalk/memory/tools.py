from __future__ import annotations

import uuid
from typing import Callable

from langchain.tools import BaseTool, tool

from .interfaces import MemoryStore
from .schema import MemoryItem


def build_memory_tools(
    *,
    memory_store: MemoryStore,
    user_id_getter: Callable[[], str | None],
    session_id_getter: Callable[[], str | None],
) -> list[BaseTool]:
    """Build user-scoped memory tools for the agent."""

    recall_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to recall."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 5,
                "description": "Maximum memories to return.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    remember_schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Memory content to store."},
            "memory_type": {
                "type": "string",
                "enum": ["semantic", "episodic", "procedural", "reflective"],
                "default": "semantic",
                "description": "Memory category.",
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    }

    forget_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Memory text to forget."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 10,
                "description": "Maximum matching memories to delete.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    @tool("recall_memory", args_schema=recall_schema)
    async def recall_memory(query: str, limit: int = 5) -> str:
        """Recall relevant long-term memory for the current user."""

        user_id = user_id_getter()
        if not user_id:
            return "Memory is unavailable for this session."
        results = await memory_store.search(
            user_id=user_id,
            query=query,
            limit=max(1, min(10, int(limit or 5))),
        )
        if not results:
            return "No relevant memory found."
        lines = [
            f"{idx}. [{result.item.type}] {result.item.content}"
            for idx, result in enumerate(results, start=1)
        ]
        return "\n".join(lines)

    @tool("remember_memory", args_schema=remember_schema)
    async def remember_memory(content: str, memory_type: str = "semantic") -> str:
        """Store an explicit user-approved long-term memory."""

        user_id = user_id_getter()
        if not user_id:
            return "Memory is unavailable for this session."
        normalized = " ".join((content or "").split())
        if not normalized:
            return "No memory content provided."
        memory_id = await memory_store.add(
            MemoryItem(
                id=str(uuid.uuid4()),
                user_id=user_id,
                session_id=session_id_getter(),
                type=memory_type if memory_type else "semantic",
                content=normalized,
                source="tool",
                confidence=1.0,
            )
        )
        return f"Memory saved: {memory_id}"

    @tool("forget_memory", args_schema=forget_schema)
    async def forget_memory(query: str, limit: int = 10) -> str:
        """Forget long-term memories matching the query for the current user."""

        user_id = user_id_getter()
        if not user_id:
            return "Memory is unavailable for this session."
        results = await memory_store.search(
            user_id=user_id,
            query=query,
            limit=max(1, min(20, int(limit or 10))),
        )
        if not results:
            return "No matching memory found."
        deleted = 0
        for result in results:
            if await memory_store.delete(user_id=user_id, memory_id=result.item.id):
                deleted += 1
        return f"Forgot {deleted} matching memories."

    recall_memory.description = (
        "Recall durable user-specific memory. Use when the user asks what you "
        "remember, refers to prior preferences, projects, plans, or personal facts."
    )
    remember_memory.description = (
        "Save explicit user-approved memory. Use when the user asks you to remember "
        "something, or states a durable preference that should persist."
    )
    forget_memory.description = (
        "Delete user-specific memory. Use when the user asks you to forget or remove "
        "something from memory."
    )
    return [recall_memory, remember_memory, forget_memory]
