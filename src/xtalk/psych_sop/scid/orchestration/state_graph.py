"""Typed orchestration state and domain events for the SCID runtime."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4

from ..core.schema import utc_now_iso
from ..state.telemetry import monotonic_ts


EVENT_SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 64 * 1024


class SessionPhase(str, Enum):
    NEW = "new"
    ACTIVE = "active"
    PAUSED = "paused"
    CRISIS = "crisis"
    COMPLETED = "completed"
    STOPPED = "stopped"
    ABORTED = "aborted"
    CLOSED = "closed"


class TurnPhase(str, Enum):
    ACCEPTED = "accepted"
    ROUTED = "routed"
    WORKERS_RUNNING = "workers_running"
    INITIAL_BOUNDARY = "initial_boundary"
    ACTION_SELECTED = "action_selected"
    DELIVERING = "delivering"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AssessmentPhase(str, Enum):
    IDLE = "idle"
    RESERVED = "reserved"
    IN_FLIGHT = "in_flight"
    VALIDATED = "validated"
    COMMITTED = "committed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DeliveryPhase(str, Enum):
    NOT_SELECTED = "not_selected"
    SELECTED = "selected"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SpeculationPhase(str, Enum):
    IDLE = "idle"
    PROPOSED = "proposed"
    SELECTED = "selected"
    SPOKEN = "spoken"
    PENDING_COMMIT = "pending_commit"
    CONFIRMED = "confirmed"
    COMPENSATING = "compensating"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class MailboxLane(str, Enum):
    CONTROL = "control"
    WORK = "work"


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Bound every hot in-memory collection for long sessions."""

    version: str = "scid_retention_v1"
    recent_interaction_turns: int = 32
    recent_runtime_turns: int = 32
    recent_latency_traces: int = 64
    recent_foreground_actions: int = 64
    recent_observer_updates: int = 64
    partial_plan_history: int = 32
    candidate_evidence: int = 128
    contextual_memories: int = 128
    candidate_cache_entries: int = 128
    recent_ledger_turns: int = 16
    control_mailbox_capacity: int = 64
    work_mailbox_capacity: int = 128
    event_queue_capacity: int = 256

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if name == "version":
                if not isinstance(value, str) or not value:
                    raise ValueError("RetentionPolicy version must be non-empty")
                continue
            if type(value) is not int or value < 1:
                raise ValueError(f"RetentionPolicy {name} must be positive")

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """Versioned orchestration policy independent from model prompts."""

    version: str = "scid_runtime_policy_v1"
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    turn_deadline_seconds: float = 30.0
    event_page_default: int = 100
    event_page_maximum: int = 500

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("RuntimePolicy version must be non-empty")
        if not math.isfinite(self.turn_deadline_seconds) or (
            self.turn_deadline_seconds <= 0
        ):
            raise ValueError("turn_deadline_seconds must be positive and finite")
        if not 1 <= self.event_page_default <= self.event_page_maximum:
            raise ValueError("event page defaults are invalid")

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "retention": self.retention.snapshot(),
            "turn_deadline_seconds": self.turn_deadline_seconds,
            "event_page_default": self.event_page_default,
            "event_page_maximum": self.event_page_maximum,
        }


@dataclass(frozen=True, slots=True)
class CausalEnvelope:
    """Causal identity attached to every asynchronous worker result."""

    interaction_seq: int
    turn_id: int | None
    field_id: str | None
    state_version: int
    generation: int
    request_id: str = field(default_factory=lambda: str(uuid4()))
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    causation_id: str | None = None
    deadline_monotonic: float | None = None

    def __post_init__(self) -> None:
        if type(self.interaction_seq) is not int or self.interaction_seq < 0:
            raise ValueError("interaction_seq must be a non-negative integer")
        if self.turn_id is not None and (
            type(self.turn_id) is not int or self.turn_id < 1
        ):
            raise ValueError("turn_id must be positive when present")
        if type(self.state_version) is not int or self.state_version < 0:
            raise ValueError("state_version must be non-negative")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be non-negative")
        if self.deadline_monotonic is not None and (
            not math.isfinite(self.deadline_monotonic) or self.deadline_monotonic < 0
        ):
            raise ValueError("deadline_monotonic must be finite")

    @classmethod
    def for_turn(
        cls,
        *,
        interaction_seq: int,
        turn_id: int | None,
        field_id: str | None,
        state_version: int,
        generation: int,
        timeout_seconds: float,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> "CausalEnvelope":
        return cls(
            interaction_seq=interaction_seq,
            turn_id=turn_id,
            field_id=field_id,
            state_version=state_version,
            generation=generation,
            correlation_id=correlation_id or str(uuid4()),
            causation_id=causation_id,
            deadline_monotonic=monotonic_ts() + timeout_seconds,
        )

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """One redacted, append-only orchestration event."""

    event_seq: int
    event_type: str
    episode_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    envelope: CausalEnvelope | None = None
    event_id: str = field(default_factory=lambda: str(uuid4()))
    occurred_at: str = field(default_factory=utc_now_iso)
    monotonic_at: float = field(default_factory=monotonic_ts)
    schema_version: int = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.event_seq) is not int or self.event_seq < 1:
            raise ValueError("event_seq must be positive")
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise ValueError("event_type must be non-empty")
        if not isinstance(self.episode_id, str) or not self.episode_id.strip():
            raise ValueError("episode_id must be non-empty")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_seq": self.event_seq,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "episode_id": self.episode_id,
            "occurred_at": self.occurred_at,
            "monotonic_at": self.monotonic_at,
            "envelope": self.envelope.snapshot() if self.envelope else None,
            "payload": _redact_event_value(dict(self.payload)),
        }


@dataclass(slots=True)
class SessionGraphState:
    """Small actor-owned state machine projection."""

    phase: SessionPhase = SessionPhase.NEW
    active_turn_seq: int | None = None
    turn_phase: TurnPhase | None = None
    assessment_phase: AssessmentPhase = AssessmentPhase.IDLE
    assessment_interaction_seq: int | None = None
    assessment_generation: int | None = None
    delivery_phase: DeliveryPhase = DeliveryPhase.NOT_SELECTED
    speculation_phase: SpeculationPhase = SpeculationPhase.IDLE
    ingress_generation: int = 0
    latest_interaction_seq: int = 0
    last_event_seq: int = 0
    total_interactions: int = 0
    total_events: int = 0
    archived_interactions: int = 0
    archived_runtime_turns: int = 0
    archived_latency_traces: int = 0
    archived_ledger_turns: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "active_turn_seq": self.active_turn_seq,
            "turn_phase": self.turn_phase.value if self.turn_phase else None,
            "assessment_phase": self.assessment_phase.value,
            "assessment_interaction_seq": self.assessment_interaction_seq,
            "assessment_generation": self.assessment_generation,
            "delivery_phase": self.delivery_phase.value,
            "speculation_phase": self.speculation_phase.value,
            "ingress_generation": self.ingress_generation,
            "latest_interaction_seq": self.latest_interaction_seq,
            "last_event_seq": self.last_event_seq,
            "total_interactions": self.total_interactions,
            "total_events": self.total_events,
            "archived_interactions": self.archived_interactions,
            "archived_runtime_turns": self.archived_runtime_turns,
            "archived_latency_traces": self.archived_latency_traces,
            "archived_ledger_turns": self.archived_ledger_turns,
        }


_SENSITIVE_EVENT_KEYS = {
    "api_key",
    "assistant_text",
    "candidate_text",
    "clarification_question",
    "content",
    "evidence",
    "instruction",
    "normalized_user_text",
    "partial_text",
    "question_text",
    "quote",
    "raw_payload",
    "raw_user_text",
    "reasoning_summary",
    "safe_frontend_content",
    "secret",
    "text",
    "token",
    "user_text",
}


def _redact_event_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.lower() in _SENSITIVE_EVENT_KEYS:
        return "[redacted]"
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_event_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_event_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(type(value).__name__)
