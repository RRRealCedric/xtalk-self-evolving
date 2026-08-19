"""Deterministic bounded memory projection for long SCID sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SessionMemoryProjection:
    """Aggregate durable session facts without retaining raw transcripts."""

    total_interactions: int = 0
    route_counts: dict[str, int] = field(default_factory=dict)
    committed_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    clarification_count: int = 0
    repair_count: int = 0
    crisis_count: int = 0

    def observe_route(self, route: str) -> None:
        """Increment aggregate interaction and route counters."""

        self.total_interactions += 1
        self.route_counts[route] = self.route_counts.get(route, 0) + 1
        if route == "crisis":
            self.crisis_count += 1

    def observe_commit(
        self,
        *,
        field_id: str | None,
        score: str | None,
        action: str,
        confidence: float,
        state_version: int,
    ) -> None:
        """Project one validated assessment outcome without free text."""

        if action in {"clarify", "reask"}:
            self.clarification_count += 1
        if field_id is None:
            return
        self.committed_fields[field_id] = {
            "field_id": field_id,
            "score": score,
            "action": action,
            "confidence": confidence,
            "state_version": state_version,
        }

    def observe_repair(self) -> None:
        """Increment the compensation-repair counter."""

        self.repair_count += 1

    def model_context(self) -> dict[str, Any]:
        """Return compact structured context for downstream models."""

        return {
            "total_interactions": self.total_interactions,
            "route_counts": dict(self.route_counts),
            "recent_committed_fields": list(self.committed_fields.values())[-12:],
            "clarification_count": self.clarification_count,
            "repair_count": self.repair_count,
            "crisis_count": self.crisis_count,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return the complete bounded projection."""

        return {
            **self.model_context(),
            "committed_field_count": len(self.committed_fields),
        }
