"""A minimal sample agent that echoes finalized ASR text."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Iterable

from xtalk.model_types import Agent
from xtalk.llm_agent import AgentContext, AgentOutput


class EchoAgent(Agent):
    """A simple agent that echoes finalized ASR text."""

    def accept(self, context: AgentContext) -> Iterable[AgentOutput]:
        """Synchronously bridge ``async_accept()`` for compatibility."""

        yield from self._sync_iter_from_async(self.async_accept(context))

    async def async_accept(
        self,
        context: AgentContext,
    ) -> AsyncIterator[AgentOutput]:
        """Emit the finalized ASR text for ``asr_final`` contexts."""

        if str(context.get("type", "") or "") != "asr_final":
            return
        payload = context.get("data") or {}
        if not isinstance(payload, dict):
            return
        text = str(payload.get("text", ""))
        if text:
            yield text

    def restore_history(self, messages: list[dict[str, Any]]) -> None:
        """Ignore persisted history for the stateless echo agent."""

        del messages
        return None

    def clone(self) -> "EchoAgent":
        """Create a fresh stateless echo agent."""

        return EchoAgent()

    def _sync_iter_from_async(
        self,
        async_iter: AsyncIterator[AgentOutput],
    ) -> Iterable[AgentOutput]:
        """Convert an async iterator into a synchronous generator."""

        loop = asyncio.new_event_loop()
        try:
            while True:
                try:
                    item = loop.run_until_complete(async_iter.__anext__())
                except StopAsyncIteration:
                    break
                yield item
        finally:
            aclose = getattr(async_iter, "aclose", None)
            if callable(aclose):
                try:
                    loop.run_until_complete(aclose())
                except Exception:
                    pass
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
