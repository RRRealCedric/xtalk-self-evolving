"""Action-conditioned foreground utterance pre-generation."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Any

from .frontend import DialogueModel
from ..core.schema import DialogueDirective
from ..state.telemetry import now_ts


logger = logging.getLogger(__name__)

CacheKey = tuple[str, int, str]


@dataclass(slots=True)
class CandidateCacheEntry:
    """One cached action-conditioned foreground utterance.

    Parameters
    ----------
    field_id : str
        SCID field for which the utterance was generated.
    state_version : int
        Committed-state version on which generation was based.
    action : str
        Foreground action represented by the utterance.
    text : str
        Rendered user-facing utterance.
    generated_at : float
        Generation timestamp.
    """

    field_id: str
    state_version: int
    action: str
    text: str
    generated_at: float

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable representation of the cache entry.

        Returns
        -------
        dict[str, Any]
            Cache-entry fields keyed by attribute name.
        """

        return asdict(self)


class CandidateUtteranceCache:
    """Cache safe frontend wording by field, state version, and action."""

    def __init__(
        self, dialogue_model: DialogueModel, *, max_entries: int = 128
    ) -> None:
        if type(max_entries) is not int or max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.dialogue_model = dialogue_model
        self.max_entries = max_entries
        self._entries: OrderedDict[CacheKey, CandidateCacheEntry] = OrderedDict()
        self._inflight: dict[CacheKey, tuple[int, asyncio.Task[str]]] = {}
        self._retired_tasks: set[asyncio.Task[str]] = set()
        self._generation = 0
        self._min_valid_state_version = 0
        self._closed = False
        self._lock = asyncio.Lock()

    async def pre_generate(
        self,
        *,
        field_id: str,
        state_version: int,
        directives: dict[str, DialogueDirective],
    ) -> dict[str, str]:
        """Generate missing candidates without changing clinical state."""

        results: dict[str, str] = {}
        pending: list[tuple[str, asyncio.Task[str]]] = []
        async with self._lock:
            for action, directive in directives.items():
                key = (field_id, state_version, action)
                if self._closed or state_version < self._min_valid_state_version:
                    results[action] = directive.question_text
                    continue
                entry = self._entries.get(key)
                if entry is not None:
                    self._entries.move_to_end(key)
                    results[action] = entry.text
                    continue
                inflight = self._inflight.get(key)
                if inflight is not None and inflight[0] == self._generation:
                    task = inflight[1]
                else:
                    generation = self._generation
                    task = asyncio.create_task(
                        self._render_and_cache(
                            key=key,
                            directive=directive,
                            generation=generation,
                        )
                    )
                    self._inflight[key] = (generation, task)
                pending.append((action, task))

        if pending:
            rendered = await asyncio.gather(
                *(asyncio.shield(task) for _, task in pending)
            )
            results.update(
                (action, text) for (action, _), text in zip(pending, rendered)
            )
        return results

    async def _render_and_cache(
        self,
        *,
        key: CacheKey,
        directive: DialogueDirective,
        generation: int,
    ) -> str:
        """Render one key and publish it only if its generation is still valid."""

        cacheable = False
        try:
            rendered, generation_succeeded = (
                await self.dialogue_model.render_cache_candidate(directive)
            )
            text = str(rendered or "").strip()
            cacheable = generation_succeeded and bool(text)
            if not text:
                text = directive.question_text
        except asyncio.CancelledError:
            await self._discard_inflight(key=key, generation=generation)
            raise
        except Exception as exc:
            logger.warning(
                "SCID candidate rendering failed field=%s state=%s action=%s "
                "error_type=%s",
                key[0],
                key[1],
                key[2],
                type(exc).__name__,
            )
            text = directive.question_text

        current_task = asyncio.current_task()
        async with self._lock:
            inflight = self._inflight.get(key)
            is_current = (
                inflight is not None
                and inflight[0] == generation
                and inflight[1] is current_task
            )
            if (
                cacheable
                and is_current
                and generation == self._generation
                and key[1] >= self._min_valid_state_version
            ):
                self._entries[key] = CandidateCacheEntry(
                    field_id=key[0],
                    state_version=key[1],
                    action=key[2],
                    text=text,
                    generated_at=now_ts(),
                )
                self._entries.move_to_end(key)
                while len(self._entries) > self.max_entries:
                    self._entries.popitem(last=False)
            if is_current:
                self._inflight.pop(key, None)
        return text

    async def _discard_inflight(self, *, key: CacheKey, generation: int) -> None:
        """Forget a cancelled render without removing a newer generation."""

        current_task = asyncio.current_task()
        async with self._lock:
            inflight = self._inflight.get(key)
            if (
                inflight is not None
                and inflight[0] == generation
                and inflight[1] is current_task
            ):
                self._inflight.pop(key, None)

    async def pre_generate_action(
        self,
        *,
        field_id: str,
        state_version: int,
        action: str,
        directive: DialogueDirective,
    ) -> str:
        """Pre-generate one action-conditioned utterance."""

        values = await self.pre_generate(
            field_id=field_id,
            state_version=state_version,
            directives={action: directive},
        )
        return values[action]

    def get(self, *, field_id: str, state_version: int, action: str) -> str | None:
        """Return a cached utterance for an exact state/action key.

        Parameters
        ----------
        field_id : str
            SCID field associated with the utterance.
        state_version : int
            Committed-state version used during generation.
        action : str
            Foreground action represented by the utterance.

        Returns
        -------
        str | None
            Cached utterance, or ``None`` when the key is absent.
        """

        entry = self._entries.get((field_id, state_version, action))
        if state_version < self._min_valid_state_version:
            return None
        if entry is not None:
            self._entries.move_to_end((field_id, state_version, action))
        return entry.text if entry is not None else None

    def invalidate_before(self, state_version: int) -> None:
        """Drop candidates based on committed states older than this version."""

        self._min_valid_state_version = max(
            self._min_valid_state_version, state_version
        )
        stale_tasks = [
            task
            for key, (_, task) in self._inflight.items()
            if key[1] < self._min_valid_state_version
        ]
        self._inflight = {
            key: value
            for key, value in self._inflight.items()
            if key[1] >= self._min_valid_state_version
        }
        self._entries = OrderedDict(
            (key, value)
            for key, value in self._entries.items()
            if value.state_version >= state_version
        )
        self._retire_tasks(stale_tasks)
        for task in stale_tasks:
            task.cancel()

    def clear(self) -> None:
        """Remove all cached utterances."""

        inflight_tasks = [task for _, task in self._inflight.values()]
        self._generation += 1
        self._inflight.clear()
        self._entries.clear()
        self._retire_tasks(inflight_tasks)
        for task in inflight_tasks:
            task.cancel()

    async def aclose(self) -> None:
        """Cancel and reap all in-flight candidate generations."""

        async with self._lock:
            self._closed = True
            self._generation += 1
            inflight_tasks = [task for _, task in self._inflight.values()]
            self._retire_tasks(inflight_tasks)
            owned_tasks = list(self._retired_tasks)
            self._inflight.clear()
            self._entries.clear()
        for task in owned_tasks:
            task.cancel()
        if owned_tasks:
            await asyncio.gather(*owned_tasks, return_exceptions=True)
            for task in owned_tasks:
                self._retired_tasks.discard(task)

    def _retire_tasks(self, tasks: list[asyncio.Task[str]]) -> None:
        """Keep invalidated tasks reachable until they actually terminate."""

        for task in tasks:
            if task in self._retired_tasks:
                continue
            self._retired_tasks.add(task)
            task.add_done_callback(self._retired_tasks.discard)

    def snapshot(self) -> list[dict[str, Any]]:
        """Return serializable cache entries in deterministic key order.

        Returns
        -------
        list[dict[str, Any]]
            Snapshots of all cached utterances.
        """

        return [
            entry.snapshot()
            for _, entry in sorted(self._entries.items(), key=lambda item: item[0])
        ]
