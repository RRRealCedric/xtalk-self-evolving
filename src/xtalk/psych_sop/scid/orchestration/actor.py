"""Single-writer session actor for SCID orchestration mutations."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Generic, TypeVar

from .event_store import EpisodeEventStore
from .state_graph import (
    AssessmentPhase,
    CausalEnvelope,
    DeliveryPhase,
    DomainEvent,
    MailboxLane,
    SessionGraphState,
    SessionPhase,
    SpeculationPhase,
    TurnPhase,
)

if TYPE_CHECKING:
    from ..state.blackboard import ClinicalBlackboard


T = TypeVar("T")


_TURN_TRANSITIONS: dict[TurnPhase, set[TurnPhase]] = {
    TurnPhase.ACCEPTED: {
        TurnPhase.ROUTED,
        TurnPhase.COMPLETED,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    },
    TurnPhase.ROUTED: {
        TurnPhase.WORKERS_RUNNING,
        TurnPhase.INITIAL_BOUNDARY,
        TurnPhase.COMPLETED,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    },
    TurnPhase.WORKERS_RUNNING: {
        TurnPhase.INITIAL_BOUNDARY,
        TurnPhase.ACTION_SELECTED,
        TurnPhase.DELIVERING,
        TurnPhase.COMPLETED,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    },
    TurnPhase.INITIAL_BOUNDARY: {
        TurnPhase.ACTION_SELECTED,
        TurnPhase.COMPLETED,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    },
    TurnPhase.ACTION_SELECTED: {
        TurnPhase.DELIVERING,
        TurnPhase.COMPLETED,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    },
    TurnPhase.DELIVERING: {
        TurnPhase.COMPLETED,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    },
    TurnPhase.COMPLETED: set(),
    TurnPhase.CANCELLED: set(),
    TurnPhase.FAILED: set(),
}

_ASSESSMENT_TRANSITIONS: dict[AssessmentPhase, set[AssessmentPhase]] = {
    AssessmentPhase.IDLE: {AssessmentPhase.RESERVED, AssessmentPhase.IN_FLIGHT},
    AssessmentPhase.RESERVED: {
        AssessmentPhase.IN_FLIGHT,
        AssessmentPhase.CANCELLED,
        AssessmentPhase.FAILED,
    },
    AssessmentPhase.IN_FLIGHT: {
        AssessmentPhase.VALIDATED,
        AssessmentPhase.COMMITTED,
        AssessmentPhase.REJECTED,
        AssessmentPhase.CANCELLED,
        AssessmentPhase.FAILED,
    },
    AssessmentPhase.VALIDATED: {
        AssessmentPhase.COMMITTED,
        AssessmentPhase.REJECTED,
        AssessmentPhase.FAILED,
    },
    AssessmentPhase.COMMITTED: {
        AssessmentPhase.RESERVED,
        AssessmentPhase.IN_FLIGHT,
    },
    AssessmentPhase.REJECTED: {
        AssessmentPhase.RESERVED,
        AssessmentPhase.IN_FLIGHT,
    },
    AssessmentPhase.CANCELLED: {
        AssessmentPhase.RESERVED,
        AssessmentPhase.IN_FLIGHT,
    },
    AssessmentPhase.FAILED: {
        AssessmentPhase.RESERVED,
        AssessmentPhase.IN_FLIGHT,
    },
}

_SESSION_TRANSITIONS: dict[SessionPhase, set[SessionPhase]] = {
    SessionPhase.NEW: {SessionPhase.ACTIVE, SessionPhase.ABORTED},
    SessionPhase.ACTIVE: {
        SessionPhase.CRISIS,
        SessionPhase.COMPLETED,
        SessionPhase.STOPPED,
        SessionPhase.ABORTED,
        SessionPhase.CLOSED,
    },
    SessionPhase.CRISIS: {SessionPhase.CLOSED},
    SessionPhase.COMPLETED: {SessionPhase.CLOSED},
    SessionPhase.STOPPED: {SessionPhase.CLOSED},
    SessionPhase.ABORTED: {SessionPhase.CLOSED},
    SessionPhase.CLOSED: set(),
}

_DELIVERY_TRANSITIONS: dict[DeliveryPhase, set[DeliveryPhase]] = {
    DeliveryPhase.NOT_SELECTED: {
        DeliveryPhase.SELECTED,
        DeliveryPhase.COMPLETED,
        DeliveryPhase.FAILED,
        DeliveryPhase.CANCELLED,
    },
    DeliveryPhase.SELECTED: {
        DeliveryPhase.NOT_SELECTED,
        DeliveryPhase.STARTED,
        DeliveryPhase.COMPLETED,
        DeliveryPhase.FAILED,
        DeliveryPhase.CANCELLED,
    },
    DeliveryPhase.STARTED: {
        DeliveryPhase.NOT_SELECTED,
        DeliveryPhase.COMPLETED,
        DeliveryPhase.FAILED,
        DeliveryPhase.CANCELLED,
    },
    DeliveryPhase.COMPLETED: {
        DeliveryPhase.NOT_SELECTED,
        DeliveryPhase.SELECTED,
    },
    DeliveryPhase.FAILED: {
        DeliveryPhase.NOT_SELECTED,
        DeliveryPhase.SELECTED,
    },
    DeliveryPhase.CANCELLED: {
        DeliveryPhase.NOT_SELECTED,
        DeliveryPhase.SELECTED,
    },
}


@dataclass(slots=True)
class _ActorMessage(Generic[T]):
    operation: str
    mutation: Callable[[], T]
    future: asyncio.Future[T]
    lane: MailboxLane
    event_type: str | None = None
    event_payload: dict[str, Any] | None = None
    envelope: CausalEnvelope | None = None
    coalesce_key: str | None = None
    event_predicate: Callable[[T], bool] | None = None


class SessionActor:
    """Serialize all authoritative per-session state mutations."""

    def __init__(
        self,
        *,
        episode_id: str,
        event_store: EpisodeEventStore,
        blackboard: ClinicalBlackboard | None = None,
        control_capacity: int = 64,
        work_capacity: int = 128,
    ) -> None:
        self.episode_id = episode_id
        self.event_store = event_store
        self.blackboard = blackboard
        self.state = SessionGraphState()
        self._control_capacity = control_capacity
        self._work_capacity = work_capacity
        self._control: deque[_ActorMessage[Any]] = deque()
        self._work: deque[_ActorMessage[Any]] = deque()
        self._condition = asyncio.Condition()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False
        self._closed = False
        self._ingress_generation = 0
        self._processed_commands = 0
        self._dropped_work_messages = 0
        self._persistence_error: str | None = None

    @property
    def ingress_generation(self) -> int:
        return self._ingress_generation

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def control_mailbox_near_capacity(self) -> bool:
        """Return whether ordinary turn admission should be paused."""

        reserve = max(1, self._control_capacity // 8)
        threshold = max(1, self._control_capacity - reserve)
        return len(self._control) >= threshold

    @property
    def normal_admission_block_reason(self) -> str | None:
        """Return the deterministic reason ordinary turns cannot be admitted."""

        if self._persistence_error or self.event_store.write_error:
            return "persistence_unavailable"
        if self.control_mailbox_near_capacity:
            return "control_mailbox_near_capacity"
        return None

    def advance_ingress_generation(self) -> int:
        """Synchronously invalidate work from every older ASR generation."""

        if self._closed:
            return self._ingress_generation
        self._ingress_generation += 1
        if self.blackboard is not None:
            self.blackboard.next_observer_version()
        return self._ingress_generation

    def invalidate_observer_generation(self) -> int:
        """Synchronously invalidate Observer work at an ingress boundary."""

        if self._closed or self.blackboard is None:
            return 0
        return self.blackboard.next_observer_version()

    def reject_partial_input(self, reason: str) -> None:
        """Synchronously invalidate one edge-rejected ASR partial plan."""

        if self._closed or self.blackboard is None:
            return
        self.blackboard.next_observer_version()
        self.blackboard.reject_partial_plan(reason)
        self.blackboard.stable_partial_text = ""

    def is_current_generation(self, generation: int) -> bool:
        return generation == self._ingress_generation and not self._closed

    async def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("SessionActor is closed")
        await self.event_store.start()
        self._task = asyncio.create_task(
            self._run(),
            name=f"scid-session-actor-{self.episode_id}",
        )
        self._started = True
        await self.set_session_phase(
            SessionPhase.ACTIVE,
            event_type="SessionStarted",
        )

    async def call(
        self,
        operation: str,
        mutation: Callable[[], T],
        *,
        lane: MailboxLane = MailboxLane.CONTROL,
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
        envelope: CausalEnvelope | None = None,
        coalesce_key: str | None = None,
        event_predicate: Callable[[T], bool] | None = None,
    ) -> T:
        if not self._started:
            await self.start()
        if (
            self._closing
            or self._closed
            or (self._task is not None and self._task.done())
        ):
            raise RuntimeError("SessionActor is closing")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        message = _ActorMessage(
            operation=operation,
            mutation=mutation,
            future=future,
            lane=lane,
            event_type=event_type,
            event_payload=event_payload,
            envelope=envelope,
            coalesce_key=coalesce_key,
            event_predicate=event_predicate,
        )
        await self._enqueue(message)
        return await future

    async def emit(
        self,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        envelope: CausalEnvelope | None = None,
        lane: MailboxLane = MailboxLane.CONTROL,
        coalesce_key: str | None = None,
    ) -> None:
        await self.call(
            f"emit:{event_type}",
            lambda: None,
            lane=lane,
            event_type=event_type,
            event_payload=payload,
            envelope=envelope,
            coalesce_key=coalesce_key,
        )

    async def set_session_phase(
        self,
        phase: SessionPhase,
        *,
        event_type: str = "SessionPhaseChanged",
        envelope: CausalEnvelope | None = None,
    ) -> None:
        def mutation() -> None:
            current = self.state.phase
            if phase is not current and phase not in _SESSION_TRANSITIONS[current]:
                raise RuntimeError(
                    f"Invalid session transition: {current.value} -> {phase.value}"
                )
            self.state.phase = phase

        await self.call(
            "set_session_phase",
            mutation,
            event_type=event_type,
            event_payload={"phase": phase.value},
            envelope=envelope,
        )

    async def set_turn_phase(
        self,
        phase: TurnPhase,
        *,
        interaction_seq: int,
        envelope: CausalEnvelope | None = None,
    ) -> None:
        def mutation() -> bool:
            if interaction_seq < self.state.latest_interaction_seq:
                return False
            if interaction_seq > self.state.latest_interaction_seq:
                if phase is not TurnPhase.ACCEPTED:
                    raise RuntimeError("A new turn must enter through accepted")
            elif (
                self.state.turn_phase is not None and phase is not self.state.turn_phase
            ):
                allowed = _TURN_TRANSITIONS.get(self.state.turn_phase, set())
                if phase not in allowed:
                    raise RuntimeError(
                        "Invalid turn transition: "
                        f"{self.state.turn_phase.value} -> {phase.value}"
                    )
            self.state.active_turn_seq = interaction_seq
            self.state.turn_phase = phase
            self.state.latest_interaction_seq = max(
                self.state.latest_interaction_seq,
                interaction_seq,
            )
            self.state.ingress_generation = self._ingress_generation
            if phase is TurnPhase.ACCEPTED:
                self.state.total_interactions += 1
                self.transition_delivery_state(DeliveryPhase.NOT_SELECTED)
            return True

        await self.call(
            "set_turn_phase",
            mutation,
            event_type="TurnPhaseChanged",
            event_payload={"phase": phase.value},
            envelope=envelope,
            event_predicate=bool,
        )

    async def set_assessment_phase(
        self,
        phase: AssessmentPhase,
        *,
        envelope: CausalEnvelope | None = None,
    ) -> None:
        await self.call(
            "set_assessment_phase",
            lambda: self.transition_assessment_state(
                phase,
                envelope=envelope,
            ),
            event_type="AssessmentPhaseChanged",
            event_payload={"phase": phase.value},
            envelope=envelope,
            event_predicate=bool,
        )

    async def set_delivery_phase(
        self,
        phase: DeliveryPhase,
        *,
        envelope: CausalEnvelope | None = None,
    ) -> None:
        await self.call(
            "set_delivery_phase",
            lambda: self.transition_delivery_state(phase),
            event_type="DeliveryPhaseChanged",
            event_payload={"phase": phase.value},
            envelope=envelope,
        )

    async def set_speculation_phase(
        self,
        phase: SpeculationPhase,
        *,
        envelope: CausalEnvelope | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self.call(
            "set_speculation_phase",
            lambda: setattr(self.state, "speculation_phase", phase),
            event_type="SagaTransitioned",
            event_payload={"phase": phase.value, **(payload or {})},
            envelope=envelope,
        )

    def transition_assessment_state(
        self,
        phase: AssessmentPhase,
        *,
        envelope: CausalEnvelope | None = None,
    ) -> bool:
        """Apply one validated assessment transition inside an Actor command."""

        if phase is AssessmentPhase.RESERVED:
            self.state.assessment_phase = phase
            if envelope is not None:
                self.state.assessment_interaction_seq = envelope.interaction_seq
                self.state.assessment_generation = envelope.generation
            return True
        if envelope is not None and not self.assessment_envelope_is_current(envelope):
            return False
        current = self.state.assessment_phase
        if phase is not current and phase not in _ASSESSMENT_TRANSITIONS[current]:
            raise RuntimeError(
                f"Invalid assessment transition: {current.value} -> {phase.value}"
            )
        self.state.assessment_phase = phase
        return True

    def assessment_envelope_is_current(self, envelope: CausalEnvelope) -> bool:
        """Return whether an assessment command owns the active transaction."""

        return (
            self.state.assessment_interaction_seq == envelope.interaction_seq
            and self.state.assessment_generation == envelope.generation
        )

    def transition_delivery_state(self, phase: DeliveryPhase) -> bool:
        """Apply one validated delivery transition inside an Actor command."""

        current = self.state.delivery_phase
        if phase is not current and phase not in _DELIVERY_TRANSITIONS[current]:
            raise RuntimeError(
                f"Invalid delivery transition: {current.value} -> {phase.value}"
            )
        self.state.delivery_phase = phase
        return True

    async def close(self, *, phase: SessionPhase = SessionPhase.CLOSED) -> None:
        if self._closed:
            return
        if self._started and not self._closing:
            await self.set_session_phase(phase, event_type="SessionTerminated")
        async with self._condition:
            self._closing = True
            self._condition.notify_all()
        if self._task is not None:
            await self._task
        try:
            await self.event_store.close()
        finally:
            self._closed = True
            self._task = None

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.state.snapshot(),
            "control_mailbox_size": len(self._control),
            "control_mailbox_capacity": self._control_capacity,
            "control_mailbox_near_capacity": (self.control_mailbox_near_capacity),
            "work_mailbox_size": len(self._work),
            "work_mailbox_capacity": self._work_capacity,
            "processed_commands": self._processed_commands,
            "dropped_work_messages": self._dropped_work_messages,
            "actor_active": bool(self._task is not None and not self._task.done()),
            "persistence_error": self._persistence_error,
        }

    async def _enqueue(self, message: _ActorMessage[Any]) -> None:
        async with self._condition:
            target = (
                self._control if message.lane is MailboxLane.CONTROL else self._work
            )
            capacity = (
                self._control_capacity
                if message.lane is MailboxLane.CONTROL
                else self._work_capacity
            )
            if message.lane is MailboxLane.WORK and message.coalesce_key:
                for index, existing in enumerate(target):
                    if existing.coalesce_key == message.coalesce_key:
                        target[index] = message
                        if not existing.future.done():
                            existing.future.set_result(None)
                        self._dropped_work_messages += 1
                        self._condition.notify_all()
                        return
            while len(target) >= capacity and not self._closing:
                if message.lane is MailboxLane.WORK:
                    dropped = target.popleft()
                    if not dropped.future.done():
                        dropped.future.set_result(None)
                    self._dropped_work_messages += 1
                    break
                await self._condition.wait()
            if self._closing:
                raise RuntimeError("SessionActor is closing")
            target.append(message)
            self._condition.notify_all()

    async def _next_message(self) -> _ActorMessage[Any] | None:
        async with self._condition:
            while not self._control and not self._work:
                if self._closing:
                    return None
                await self._condition.wait()
            message = self._control.popleft() if self._control else self._work.popleft()
            self._condition.notify_all()
            return message

    async def _run(self) -> None:
        current: _ActorMessage[Any] | None = None
        try:
            while True:
                current = await self._next_message()
                if current is None:
                    return
                try:
                    result = current.mutation()
                    self._processed_commands += 1
                    should_emit = current.event_type is not None and (
                        current.event_predicate is None
                        or current.event_predicate(result)
                    )
                    if should_emit:
                        event = self._build_event(
                            current.event_type or "",
                            payload={
                                "operation": current.operation,
                                **(current.event_payload or {}),
                            },
                            envelope=current.envelope,
                        )
                        try:
                            await self.event_store.append(event)
                        except Exception as exc:
                            self._persistence_error = type(exc).__name__
                    if not current.future.done():
                        current.future.set_result(result)
                except Exception as exc:
                    failure = self._build_event(
                        "OperationFailed",
                        payload={
                            "operation": current.operation,
                            "error_code": "actor_mutation_failed",
                            "exception_type": type(exc).__name__,
                            "retryable": False,
                        },
                        envelope=current.envelope,
                    )
                    try:
                        await self.event_store.append(failure)
                    except Exception as persistence_exc:
                        self._persistence_error = type(persistence_exc).__name__
                    if not current.future.done():
                        current.future.set_exception(exc)
                finally:
                    current = None
        except asyncio.CancelledError:
            self._closing = True
            if current is not None and not current.future.done():
                current.future.cancel()
            await self._cancel_pending_messages()
            raise

    async def _cancel_pending_messages(self) -> None:
        """Cancel callers waiting on commands the Actor cannot execute."""

        async with self._condition:
            for queue in (self._control, self._work):
                while queue:
                    message = queue.popleft()
                    if not message.future.done():
                        message.future.cancel()
            self._condition.notify_all()

    def _build_event(
        self,
        event_type: str,
        *,
        payload: dict[str, Any],
        envelope: CausalEnvelope | None,
    ) -> DomainEvent:
        event_seq = self.state.last_event_seq + 1
        self.state.last_event_seq = event_seq
        self.state.total_events += 1
        return DomainEvent(
            event_seq=event_seq,
            event_type=event_type,
            episode_id=self.episode_id,
            payload=payload,
            envelope=envelope,
        )
