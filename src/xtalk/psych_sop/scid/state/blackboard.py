"""Candidate-state blackboard shared by SCID background components."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.schema import TurnInterpretation, utc_now_iso


@dataclass(slots=True)
class SpeculativeAdvance:
    """At most one foreground field shown before its source field commits."""

    source_field_id: str
    speculative_field_id: str
    source_interaction_seq: int
    based_on_state_version: int
    question_text: str
    deferred_user_text: str = ""
    deferred_interaction_seq: int | None = None
    status: str = "pending"
    created_at: str = field(default_factory=utc_now_iso)

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable snapshot of the speculative advance.

        Returns
        -------
        dict[str, Any]
            Mapping containing every dataclass field.
        """

        return asdict(self)


@dataclass(slots=True)
class PartialObserverPlan:
    """Observer action prepared from stable ASR partial text."""

    interaction_seq: int
    field_id: str | None
    based_on_state_version: int
    observer_version: int
    partial_text: str
    interpretation: TurnInterpretation
    ready_at: float
    candidate_texts: dict[str, str] = field(default_factory=dict)
    status: str = "ready"
    promoted_at: float | None = None
    rejection_reason: str = ""

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable snapshot of the partial observer plan.

        Returns
        -------
        dict[str, Any]
            Plan fields with the interpretation converted to its snapshot.
        """

        payload = asdict(self)
        payload["interpretation"] = self.interpretation.snapshot()
        return payload


@dataclass(slots=True)
class ClinicalBlackboard:
    """Separate provisional interpretation from committed ledger state."""

    committed_state_version: int = 0
    current_field_id: str | None = None
    latest_interaction_seq: int = 0
    latest_user_text: str = ""
    stable_partial_text: str = ""
    observer_version: int = 0
    assessor_status: str = "idle"
    assessor_field_id: str | None = None
    candidate_evidence: list[dict[str, Any]] = field(default_factory=list)
    contextual_memories: list[dict[str, Any]] = field(default_factory=list)
    observer_updates: list[dict[str, Any]] = field(default_factory=list)
    latest_partial_plan: PartialObserverPlan | None = None
    partial_plan_history: list[dict[str, Any]] = field(default_factory=list)
    pending_foreground_probe: dict[str, Any] | None = None
    spoken_action: dict[str, Any] | None = None
    speculative_advance: SpeculativeAdvance | None = None
    repair_pending: dict[str, Any] | None = None

    def sync_committed_state(
        self,
        *,
        state_version: int,
        current_field_id: str | None,
    ) -> None:
        """Synchronize blackboard pointers with committed ledger state.

        Parameters
        ----------
        state_version : int
            Current committed ledger version.
        current_field_id : str | None
            Identifier of the active committed field, if any.
        """

        self.committed_state_version = state_version
        self.current_field_id = current_field_id

    def begin_interaction(self, interaction_seq: int, user_text: str) -> None:
        """Record the newest observed user interaction.

        Parameters
        ----------
        interaction_seq : int
            Sequence number assigned to the interaction.
        user_text : str
            User text associated with the interaction.
        """

        self.latest_interaction_seq = max(self.latest_interaction_seq, interaction_seq)
        self.latest_user_text = user_text

    def next_observer_version(self) -> int:
        """Advance and return the observer version counter.

        Returns
        -------
        int
            Incremented observer version.
        """

        self.observer_version += 1
        return self.observer_version

    def apply_observation(self, interpretation: TurnInterpretation) -> bool:
        """Record an observer result and return whether it is current."""

        is_current = (
            interpretation.observer_version == self.observer_version
            and interpretation.based_on_state_version == self.committed_state_version
        )
        interpretation.stale = not is_current
        snapshot = interpretation.snapshot()
        self.observer_updates.append(snapshot)
        self.observer_updates = self.observer_updates[-100:]
        # Contextual history remains useful even when an action prediction is
        # stale. It stays explicitly context-only and never enters the ledger.
        self.contextual_memories.extend(interpretation.contextual_memories)
        self.contextual_memories = self.contextual_memories[-100:]
        if not is_current:
            return False

        self.candidate_evidence.extend(interpretation.evidence_candidates)
        self.candidate_evidence = self.candidate_evidence[-100:]
        return True

    def set_partial_plan(self, plan: PartialObserverPlan) -> None:
        """Replace the latest reusable partial plan."""

        previous = self.latest_partial_plan
        if previous is not None and previous is not plan and previous.status == "ready":
            previous.status = "rejected"
            previous.rejection_reason = "superseded_by_new_partial"
            self.partial_plan_history.append(previous.snapshot())
        self.latest_partial_plan = plan
        self.partial_plan_history.append(plan.snapshot())
        self.partial_plan_history = self.partial_plan_history[-100:]

    def promote_partial_plan(self, *, promoted_at: float) -> PartialObserverPlan | None:
        """Promote the latest partial plan for reuse by a final transcript.

        Parameters
        ----------
        promoted_at : float
            Timestamp at which the plan was promoted.

        Returns
        -------
        PartialObserverPlan | None
            Promoted plan, or ``None`` when no partial plan is available.
        """

        plan = self.latest_partial_plan
        if plan is None:
            return None
        plan.status = "promoted"
        plan.promoted_at = promoted_at
        plan.rejection_reason = ""
        self.partial_plan_history.append(plan.snapshot())
        self.partial_plan_history = self.partial_plan_history[-100:]
        return plan

    def reject_partial_plan(self, reason: str) -> None:
        """Reject and clear the latest partial observer plan.

        Parameters
        ----------
        reason : str
            Reason recorded on the rejected plan.
        """

        plan = self.latest_partial_plan
        if plan is None:
            return
        plan.status = "rejected"
        plan.rejection_reason = reason
        self.partial_plan_history.append(plan.snapshot())
        self.partial_plan_history = self.partial_plan_history[-100:]
        self.latest_partial_plan = None

    def clear_partial_plan(self) -> None:
        """Clear the latest partial observer plan without changing history."""

        self.latest_partial_plan = None

    def mark_spoken_action(self, payload: dict[str, Any]) -> None:
        """Record metadata for the most recently spoken foreground action.

        Parameters
        ----------
        payload : dict[str, Any]
            Action metadata to copy into the blackboard.
        """

        self.spoken_action = dict(payload)

    def request_foreground_probe(self, payload: dict[str, Any]) -> None:
        """Hold the source answer while a same-field Observer probe is asked."""

        self.pending_foreground_probe = dict(payload)

    def clear_foreground_probe(self) -> None:
        """Clear the pending same-field foreground probe."""

        self.pending_foreground_probe = None

    @property
    def speculative_depth(self) -> int:
        """Return the number of active speculative advances.

        Returns
        -------
        int
            ``1`` when an advance is active; otherwise ``0``.
        """

        return 1 if self.speculative_advance is not None else 0

    def begin_speculation(
        self,
        *,
        source_field_id: str,
        speculative_field_id: str,
        source_interaction_seq: int,
        based_on_state_version: int,
        question_text: str,
    ) -> SpeculativeAdvance:
        """Start a single speculative advance to another field.

        Parameters
        ----------
        source_field_id : str
            Field whose assessment has not yet committed.
        speculative_field_id : str
            Field shown optimistically in the foreground.
        source_interaction_seq : int
            Interaction sequence that triggered the advance.
        based_on_state_version : int
            Committed state version on which the advance is based.
        question_text : str
            Foreground question shown for the speculative field.

        Returns
        -------
        SpeculativeAdvance
            Newly active speculative-advance state.

        Raises
        ------
        RuntimeError
            If another speculative advance is already active.
        """

        if self.speculative_advance is not None:
            raise RuntimeError("Only one speculative SCID field is allowed")
        state = SpeculativeAdvance(
            source_field_id=source_field_id,
            speculative_field_id=speculative_field_id,
            source_interaction_seq=source_interaction_seq,
            based_on_state_version=based_on_state_version,
            question_text=question_text,
        )
        self.speculative_advance = state
        return state

    def defer_speculative_reply(self, *, interaction_seq: int, user_text: str) -> None:
        """Buffer a user reply received during a speculative advance.

        Parameters
        ----------
        interaction_seq : int
            Sequence number of the deferred interaction.
        user_text : str
            Reply text to retain until speculation is resolved.

        Raises
        ------
        RuntimeError
            If no speculative advance is active.
        """

        state = self.speculative_advance
        if state is None:
            raise RuntimeError("No speculative SCID field is active")
        if state.deferred_user_text and user_text not in state.deferred_user_text:
            state.deferred_user_text = f"{state.deferred_user_text}{user_text}"
        else:
            state.deferred_user_text = user_text
        state.deferred_interaction_seq = interaction_seq

    def confirm_speculation(self) -> None:
        """Confirm the active speculation and clear resolved repair state."""

        if self.speculative_advance is not None:
            self.speculative_advance.status = "confirmed"
            if not self.speculative_advance.deferred_user_text:
                self.speculative_advance = None
        self.repair_pending = None

    def complete_speculation(self) -> None:
        """Clear all active speculation and repair state."""

        self.speculative_advance = None
        self.repair_pending = None

    def request_repair(self, payload: dict[str, Any]) -> None:
        """Mark speculation for repair and record repair metadata.

        Parameters
        ----------
        payload : dict[str, Any]
            Repair metadata to copy into the blackboard.
        """

        if self.speculative_advance is not None:
            self.speculative_advance.status = "repair_pending"
        previous_announced = bool((self.repair_pending or {}).get("announced"))
        self.repair_pending = dict(payload)
        if previous_announced:
            self.repair_pending["announced"] = True

    def clear_repair(self) -> None:
        """Clear repair metadata and the associated speculative advance."""

        self.repair_pending = None
        self.speculative_advance = None

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable snapshot of provisional blackboard state.

        Returns
        -------
        dict[str, Any]
            Copies of blackboard collections and nested state snapshots.
        """

        return {
            "committed_state_version": self.committed_state_version,
            "current_field_id": self.current_field_id,
            "latest_interaction_seq": self.latest_interaction_seq,
            "latest_user_text": self.latest_user_text,
            "stable_partial_text": self.stable_partial_text,
            "observer_version": self.observer_version,
            "assessor_status": self.assessor_status,
            "assessor_field_id": self.assessor_field_id,
            "candidate_evidence": list(self.candidate_evidence),
            "contextual_memories": list(self.contextual_memories),
            "observer_updates": list(self.observer_updates),
            "latest_partial_plan": (
                self.latest_partial_plan.snapshot()
                if self.latest_partial_plan is not None
                else None
            ),
            "partial_plan_history": list(self.partial_plan_history),
            "pending_foreground_probe": self.pending_foreground_probe,
            "spoken_action": self.spoken_action,
            "speculative_depth": self.speculative_depth,
            "speculative_advance": (
                self.speculative_advance.snapshot()
                if self.speculative_advance is not None
                else None
            ),
            "repair_pending": self.repair_pending,
        }
