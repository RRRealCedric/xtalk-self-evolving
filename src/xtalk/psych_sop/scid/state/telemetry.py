"""Latency telemetry for the SCID runtime."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass


def now_ts() -> float:
    """Return a wall-clock timestamp for event correlation."""

    return time.time()


@dataclass(slots=True)
class SCIDLatencyTrace:
    """One interaction's internal SCID latency trace."""

    interaction_seq: int
    field_id: str | None
    route: str | None = None
    asr_partial_first_at: float | None = None
    asr_final_at: float | None = None
    pre_router_started_at: float | None = None
    pre_router_finished_at: float | None = None
    observer_started_at: float | None = None
    observer_action_ready_at: float | None = None
    observer_action: str | None = None
    observer_confidence: float | None = None
    observer_stale: bool = False
    partial_plan_ready_at: float | None = None
    partial_plan_promoted_at: float | None = None
    partial_plan_rejected_reason: str = ""
    candidate_generation_started_at: float | None = None
    candidate_generation_finished_at: float | None = None
    assessor_started_at: float | None = None
    assessor_finished_at: float | None = None
    assessor_action: str | None = None
    observer_assessor_agree: bool | None = None
    frontend_initial_started_at: float | None = None
    frontend_initial_finished_at: float | None = None
    frontend_followup_started_at: float | None = None
    frontend_followup_finished_at: float | None = None
    frontend_stream_started_at: float | None = None
    frontend_first_token_at: float | None = None
    foreground_action_ready_at: float | None = None
    foreground_action_source: str | None = None
    foreground_action_kind: str | None = None
    foreground_action_selected_at: float | None = None
    foreground_action_first_token_at: float | None = None
    initial_semantic_boundary_at: float | None = None
    foreground_action_superseded_count: int = 0
    speech_action_committed_at: float | None = None
    post_boundary_wait_started_at: float | None = None
    post_boundary_wait_finished_at: float | None = None
    fast_policy_started_at: float | None = None
    fast_policy_finished_at: float | None = None
    broker_wait_started_at: float | None = None
    broker_wait_finished_at: float | None = None
    ledger_committed_at: float | None = None
    first_segment_published_at: float | None = None
    followup_segment_published_at: float | None = None
    realtime_cancelled: bool = False
    speculative_advance: bool = False
    speculative_field_id: str | None = None
    speculative_cancelled: bool = False
    stale: bool = False
    repair_required: bool = False
    error: str = ""

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-friendly representation."""

        return asdict(self)
