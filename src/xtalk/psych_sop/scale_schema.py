"""Data models for structured psychology scales."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


ScaleStatus = Literal["not_started", "in_progress", "completed", "aborted"]


@dataclass(slots=True)
class ScaleOption:
    """One answer option for a scale item."""

    option_id: int
    description: str
    score: int


@dataclass(slots=True)
class ScaleSpec:
    """Normalized scale specification."""

    scale_id: str
    title: str
    description: str
    introductions: dict[str, Any] = field(default_factory=dict)
    score_interpretation: Any = field(default_factory=dict)
    questions: list[str] = field(default_factory=list)
    options: list[ScaleOption] = field(default_factory=list)
    additional_questions: list[dict[str, Any]] | None = None
    source_path: str | None = None


@dataclass(slots=True)
class ScaleSessionState:
    """Deterministic runtime state for one scale session."""

    scale_id: str
    current_index: int = 0
    answers: dict[int, int] = field(default_factory=dict)
    raw_answers: dict[int, str] = field(default_factory=dict)
    skipped: list[int] = field(default_factory=list)
    status: ScaleStatus = "not_started"
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    abort_reason: str | None = None

    def touch(self) -> None:
        """Update the state timestamp."""

        self.updated_at = datetime.utcnow().isoformat()
