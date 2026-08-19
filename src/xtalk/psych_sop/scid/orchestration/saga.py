"""Explicit compensation saga for one-step speculative advancement."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .state_graph import SpeculationPhase


_ALLOWED_TRANSITIONS: dict[SpeculationPhase, set[SpeculationPhase]] = {
    SpeculationPhase.IDLE: {SpeculationPhase.PROPOSED},
    SpeculationPhase.PROPOSED: {
        SpeculationPhase.SELECTED,
        SpeculationPhase.CANCELLED,
    },
    SpeculationPhase.SELECTED: {
        SpeculationPhase.SPOKEN,
        SpeculationPhase.CANCELLED,
    },
    SpeculationPhase.SPOKEN: {
        SpeculationPhase.PENDING_COMMIT,
        SpeculationPhase.CANCELLED,
    },
    SpeculationPhase.PENDING_COMMIT: {
        SpeculationPhase.CONFIRMED,
        SpeculationPhase.COMPENSATING,
        SpeculationPhase.CANCELLED,
    },
    SpeculationPhase.CONFIRMED: {SpeculationPhase.CLOSED},
    SpeculationPhase.COMPENSATING: {SpeculationPhase.CLOSED},
    SpeculationPhase.CANCELLED: {SpeculationPhase.CLOSED},
    SpeculationPhase.CLOSED: {SpeculationPhase.PROPOSED},
}


@dataclass(slots=True)
class SpeculationSaga:
    action_id: str | None = None
    source_interaction_seq: int | None = None
    source_field_id: str | None = None
    speculative_field_id: str | None = None
    phase: SpeculationPhase = SpeculationPhase.IDLE
    transition_version: int = 0
    last_reason: str = ""

    @property
    def active(self) -> bool:
        return self.phase not in {SpeculationPhase.IDLE, SpeculationPhase.CLOSED}

    def begin(
        self,
        *,
        action_id: str,
        source_interaction_seq: int,
        source_field_id: str,
        speculative_field_id: str,
    ) -> int:
        if self.active:
            raise RuntimeError("A speculative saga is already active")
        self.action_id = action_id
        self.source_interaction_seq = source_interaction_seq
        self.source_field_id = source_field_id
        self.speculative_field_id = speculative_field_id
        return self.transition(SpeculationPhase.PROPOSED, reason="candidate_created")

    def transition(
        self,
        phase: SpeculationPhase,
        *,
        reason: str,
        action_id: str | None = None,
        expected_version: int | None = None,
    ) -> int:
        should_transition = self.validate_transition(
            phase,
            action_id=action_id,
            expected_version=expected_version,
        )
        if not should_transition:
            return self.transition_version
        self.phase = phase
        self.transition_version += 1
        self.last_reason = reason
        return self.transition_version

    def validate_transition(
        self,
        phase: SpeculationPhase,
        *,
        action_id: str | None = None,
        expected_version: int | None = None,
    ) -> bool:
        """Validate a transition without mutating Saga state.

        Parameters
        ----------
        phase : SpeculationPhase
            Requested destination phase.
        action_id : str | None, optional
            Action identity expected to own the Saga.
        expected_version : int | None, optional
            Optimistic concurrency version supplied by the caller.

        Returns
        -------
        bool
            ``True`` when the transition would change phase and ``False`` for
            an idempotent duplicate.
        """

        if expected_version is not None and expected_version != self.transition_version:
            raise RuntimeError("Speculation saga transition version is stale")
        if action_id is not None and self.action_id not in {None, action_id}:
            raise RuntimeError("Speculation saga action_id does not match")
        if phase == self.phase:
            return False
        allowed = _ALLOWED_TRANSITIONS.get(self.phase, set())
        if phase not in allowed:
            raise RuntimeError(
                f"Invalid speculation transition: {self.phase.value} -> {phase.value}"
            )
        return True

    def reset(self) -> None:
        self.action_id = None
        self.source_interaction_seq = None
        self.source_field_id = None
        self.speculative_field_id = None
        self.phase = SpeculationPhase.IDLE
        self.transition_version = 0
        self.last_reason = ""

    def snapshot(self) -> dict[str, object]:
        payload = asdict(self)
        payload["phase"] = self.phase.value
        payload["active"] = self.active
        return payload
