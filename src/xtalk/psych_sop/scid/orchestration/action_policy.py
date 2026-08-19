"""Versioned foreground action eligibility and ranking policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..dialogue.foreground import ForegroundAction


@dataclass(frozen=True, slots=True)
class ActionEligibilityResult:
    allowed: bool
    reason_code: str


class ActionEligibilityPolicy:
    """Apply hard causal and safety gates before ranking an action."""

    def evaluate(
        self,
        action: "ForegroundAction",
        *,
        interaction_seq: int,
        state_version: int,
        field_id: str | None,
        speculation_active: bool = False,
    ) -> ActionEligibilityResult:
        if action.interaction_seq != interaction_seq:
            return ActionEligibilityResult(False, "stale_interaction")
        if action.based_on_state_version != state_version:
            return ActionEligibilityResult(False, "stale_state_version")
        if action.field_id != field_id:
            return ActionEligibilityResult(False, "stale_field")
        if action.speculative and speculation_active:
            return ActionEligibilityResult(False, "speculation_already_active")
        return ActionEligibilityResult(True, "eligible")


class ActionRankingPolicy:
    """Keep ranking deterministic and auditable."""

    version = "scid_action_ranking_v1"

    def rank(self, action: "ForegroundAction") -> tuple[int, int, str]:
        committed = int(action.source == "assessor")
        final_observer = int(action.source == "observer")
        return (action.priority, committed + final_observer, action.action_id)


@dataclass(frozen=True, slots=True)
class ActionBudget:
    max_followups_per_turn: int = 1
    max_speculative_depth: int = 1

    def __post_init__(self) -> None:
        if self.max_followups_per_turn != 1:
            raise ValueError("SCID currently permits exactly one follow-up per turn")
        if self.max_speculative_depth != 1:
            raise ValueError("SCID speculation depth is fixed at one")
