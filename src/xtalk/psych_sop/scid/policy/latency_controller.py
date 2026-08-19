"""Risk-aware action arbitration for SCID foreground latency."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..core.schema import SCIDField, TurnInterpretation


@dataclass(slots=True)
class LatencyPlan:
    """Describe the foreground action selected for the current turn.

    Attributes
    ----------
    mode : str
        Latency-control mode selected for the turn.
    allow_speculation : bool
        Whether the foreground may advance before assessor commitment.
    selected_action : str
        Action that the foreground should perform.
    reason : str
        Human-readable explanation for the selected action.
    deadline_ms : int
        Target latency budget for the foreground action, in milliseconds.
    """

    mode: str
    allow_speculation: bool
    selected_action: str
    reason: str
    deadline_ms: int = 900

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable snapshot of the latency plan.

        Returns
        -------
        dict[str, Any]
            Mapping containing every dataclass field.
        """

        return asdict(self)


class ClinicalLatencyController:
    """Gate a permitted one-step scan advance with conservative checks."""

    def __init__(self, *, observer_confidence_threshold: float = 0.9) -> None:
        self.observer_confidence_threshold = observer_confidence_threshold

    def plan(
        self,
        *,
        field: SCIDField | None,
        interpretation: TurnInterpretation,
        allow_one_step_speculation: bool,
        speculative_depth: int,
        repair_pending: bool,
    ) -> LatencyPlan:
        """Select a risk-aware foreground latency action.

        Parameters
        ----------
        field : SCIDField | None
            Active SCID field, or ``None`` when no field is active.
        interpretation : TurnInterpretation
            Provisional observer interpretation for the current turn.
        allow_one_step_speculation : bool
            Whether one-step speculative advancement is permitted at all.
        speculative_depth : int
            Number of currently active speculative advances.
        repair_pending : bool
            Whether a speculative repair must be completed first.

        Returns
        -------
        LatencyPlan
            Optimistic-advance plan when every safety gate passes; otherwise a
            plan that holds for the assessor.
        """

        if not allow_one_step_speculation:
            return self._hold("one-step speculation is disabled")
        if field is None:
            return self._hold("there is no active field")
        if field.latency_mode != "optimistic_scan" or field.kind != "scan":
            return self._hold("the current field is a conservative gate")
        if field.safety_sensitive:
            return self._hold("the current field is safety-sensitive")
        if repair_pending:
            return self._hold("a repair must finish before advancing")
        if speculative_depth >= 1:
            return self._hold("maximum speculative depth is already reached")
        if interpretation.recommended_action != "ask_next_field":
            return self._hold("observer did not recommend the next scan field")
        if interpretation.commit_required:
            return self._hold("observer marked the action as commit-required")
        if interpretation.current_field_relevance < 0.8:
            return self._hold("current-field relevance is too low")
        if interpretation.confidence < self.observer_confidence_threshold:
            return self._hold("observer confidence is below threshold")

        return LatencyPlan(
            mode="optimistic_advance",
            allow_speculation=True,
            selected_action="ask_next_field",
            reason="high-confidence low-risk scan answer",
        )

    @staticmethod
    def _hold(reason: str) -> LatencyPlan:
        return LatencyPlan(
            mode="hold_for_assessor",
            allow_speculation=False,
            selected_action="hold_for_assessor",
            reason=reason,
        )
