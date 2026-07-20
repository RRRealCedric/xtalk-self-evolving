"""Action-conditioned foreground utterance pre-generation."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from .frontend import DialogueModel
from ..core.schema import DialogueDirective
from ..state.telemetry import now_ts


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

    def __init__(self, dialogue_model: DialogueModel) -> None:
        self.dialogue_model = dialogue_model
        self._entries: dict[tuple[str, int, str], CandidateCacheEntry] = {}
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
        missing: list[tuple[str, DialogueDirective]] = []
        async with self._lock:
            for action, directive in directives.items():
                entry = self._entries.get((field_id, state_version, action))
                if entry is not None:
                    results[action] = entry.text
                else:
                    missing.append((action, directive))

        if missing:
            rendered = await asyncio.gather(
                *(self.dialogue_model.render(directive) for _, directive in missing),
                return_exceptions=True,
            )
            async with self._lock:
                for (action, directive), value in zip(missing, rendered):
                    if isinstance(value, Exception):
                        text = directive.question_text
                    else:
                        text = str(value or "").strip() or directive.question_text
                    entry = CandidateCacheEntry(
                        field_id=field_id,
                        state_version=state_version,
                        action=action,
                        text=text,
                        generated_at=now_ts(),
                    )
                    self._entries[(field_id, state_version, action)] = entry
                    results[action] = text
        return results

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
        return entry.text if entry is not None else None

    def invalidate_before(self, state_version: int) -> None:
        """Drop candidates based on committed states older than this version."""

        self._entries = {
            key: value
            for key, value in self._entries.items()
            if value.state_version >= state_version
        }

    def clear(self) -> None:
        """Remove all cached utterances."""

        self._entries.clear()

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
