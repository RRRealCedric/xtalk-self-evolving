from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


MemoryType = Literal["semantic", "episodic", "procedural", "reflective"]


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO format."""

    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class MemoryItem:
    """A single long-term memory item.

    Parameters
    ----------
    id : str
        Stable memory identifier.
    user_id : str
        Owner user identifier. All retrieval and deletion must filter by this.
    content : str
        Plain text memory content.
    type : MemoryType
        Memory category.
    session_id : str | None, optional
        Source session identifier.
    source : str, optional
        Source label such as ``explicit`` or ``debug_api``.
    confidence : float, optional
        Confidence score in the range ``0.0`` to ``1.0``.
    metadata : dict[str, Any], optional
        Extra structured metadata.
    created_at : str, optional
        UTC ISO timestamp.
    updated_at : str, optional
        UTC ISO timestamp.
    """

    id: str
    user_id: str
    content: str
    type: MemoryType = "semantic"
    session_id: str | None = None
    source: str = "explicit"
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class MemorySearchResult:
    """Search result containing a memory item and a relevance score."""

    item: MemoryItem
    score: float = 0.0
