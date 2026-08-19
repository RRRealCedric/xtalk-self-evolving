"""Orchestrator runtime for the SCID dual-LM voice assessment."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import unicodedata
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, AsyncIterator, Literal, overload
from uuid import uuid4

from ....log_utils import logger
from ...episode_logger import DEFAULT_EPISODE_DIR
from ...safety_guard import SafetyGuard
from ..assessment.backend import (
    AssessmentRequest,
    BackgroundAssessor,
    create_background_assessor,
)
from ..assessment.decision import fallback_reask_decision
from ..dialogue.candidate_cache import CandidateUtteranceCache
from ..dialogue.frontend import (
    DialogueModel,
    FrontendStreamInterrupted,
    RuleBasedDialogueModel,
)
from ..dialogue.foreground import (
    FastForegroundPolicy,
    ForegroundAction,
    ForegroundActionBroker,
)
from ..dialogue.repair import RepairRequest, build_repair_directive
from ..core.product_contract import (
    CRISIS_RESPONSE_ZH,
    DEPLOYMENT_SCOPE,
    INPUT_TOO_LONG_ZH,
    SESSION_COMPLETED_ZH,
    SESSION_STOPPED_ZH,
    PRODUCT_CONTRACT_VERSION,
    render_session_opening,
)
from ..policy.latency_controller import ClinicalLatencyController
from ..policy.observer import (
    IncrementalObserver,
    ObserverParseError,
    create_incremental_observer,
    validate_observer_provenance,
)
from ..policy.router import (
    RuleBasedSCIDInteractionRouter,
    SCIDInteractionRouter,
    create_scid_interaction_router,
)
from ..core.schema import (
    AssessmentDecision,
    DialogueDirective,
    SCIDInteractionTurn,
    SCIDRouteDecision,
    TurnInterpretation,
    utc_now_iso,
)
from ..core.template import load_scid_template
from ..state.blackboard import ClinicalBlackboard, PartialObserverPlan
from ..state.ledger import AssessmentLedger, LedgerValidationError
from ..state.telemetry import SCIDLatencyTrace, monotonic_ts, now_ts
from .actor import SessionActor
from .event_store import EpisodeEventStore
from .memory_projection import SessionMemoryProjection
from .model_gateway import ModelGateway, ModelPriority
from .saga import SpeculationSaga
from .state_graph import (
    AssessmentPhase,
    CausalEnvelope,
    DeliveryPhase,
    MailboxLane,
    RuntimePolicy,
    SessionPhase,
    SpeculationPhase,
    TurnPhase,
)
from .supervisor import SupervisedTask, TurnSupervisor


_OBSERVER_SLOT_BY_ACTION = {
    "ask_duration": "duration",
    "ask_frequency": "frequency",
    "ask_most_of_day": "most_of_day",
    "ask_impairment": "impairment",
    "clarify_time_window": "time_window",
}

_OBSERVER_PROBE_QUESTIONS = {
    "ask_duration": "你刚才说的这种情况，每次通常会持续多久？",
    "ask_frequency": "这种情况在那段时间里大概多久会出现一次？",
    "ask_most_of_day": "它出现的时候，通常会持续一天中的大部分时间吗？",
    "ask_impairment": "它有没有影响到你的工作、学习、日常生活或与人相处？",
    "clarify_time_window": (
        "我想确认一下时间范围：你刚才说的主要是最近这段时间，" "还是更早以前也出现过？"
    ),
}

_MAX_FINAL_TEXT_CHARS = 8192
_MAX_PARTIAL_TEXT_CHARS = 2048
_TASK_DRAIN_TIMEOUT_SECONDS = 0.1
_RUNTIME_BUSY_ZH = "当前处理队列有些繁忙，请稍等片刻后再说一次。"
_RUNTIME_PERSISTENCE_UNAVAILABLE_ZH = (
    "当前后台记录暂时不可用。为避免状态错乱，请稍等片刻后再继续。"
)
_PREEMPTIVE_STOP_TOKENS = (
    "退出",
    "结束评估",
    "不做了",
    "停止评估",
    "stop",
    "quit",
)


@dataclass(slots=True)
class _AssessmentOwner:
    token: str
    interaction_seq: int
    turn_id: int
    field_id: str
    state_version: int
    active: bool = True
    committed: bool = False


@dataclass(slots=True)
class _RouteResponse:
    """Internal non-streaming result for deterministic control routes."""

    final_text: str
    stale: bool = False
    interaction_seq: int | None = None


@dataclass(slots=True)
class SCIDRuntimeResponse:
    """Realtime streams emitted for one accepted user interaction."""

    interaction_seq: int
    initial_stream: AsyncIterator[str] | None
    action_stream_task: (
        asyncio.Task[AsyncIterator[str] | None]
        | SupervisedTask[AsyncIterator[str] | None]
        | None
    )
    stale: bool = False
    terminal: bool = False


class SCIDDualLMRuntime:
    """Coordinate frontend dialogue, routing, background assessment, and state."""

    def __init__(
        self,
        *,
        experiment_id: str = "scid_voice_demo",
        user_id: str = "xtalk_scid_demo_user",
        episode_dir: str | Path = DEFAULT_EPISODE_DIR,
        backend_model: str = "deepseek-v4-pro",
        observer_model: str | None = "deepseek-v4-flash",
        prefer_deepseek: bool = True,
        deepseek_api_key: str | None = None,
        deepseek_base_url: str = "https://api.deepseek.com",
        assessor: BackgroundAssessor | None = None,
        router: SCIDInteractionRouter | None = None,
        observer: IncrementalObserver | None = None,
        dialogue_model: DialogueModel | None = None,
        observer_mode: str = "shadow",
        allow_one_step_speculation: bool = False,
        observer_confidence_threshold: float = 0.9,
        partial_plan_max_age_seconds: float = 3.0,
        post_initial_action_wait_seconds: float = 0.35,
        persist_raw_transcript: bool = False,
        runtime_policy: RuntimePolicy | None = None,
        episode_id: str | None = None,
    ) -> None:
        experiment_id = _strict_runtime_string("experiment_id", experiment_id)
        user_id = _strict_runtime_string("user_id", user_id)
        backend_model = _strict_runtime_string("backend_model", backend_model)
        observer_model = _strict_runtime_string(
            "observer_model",
            observer_model or "deepseek-v4-flash",
        )
        deepseek_base_url = _strict_runtime_string(
            "deepseek_base_url",
            deepseek_base_url,
        )
        prefer_deepseek = _strict_runtime_bool(
            "prefer_deepseek",
            prefer_deepseek,
        )
        allow_one_step_speculation = _strict_runtime_bool(
            "allow_one_step_speculation",
            allow_one_step_speculation,
        )
        persist_raw_transcript = _strict_runtime_bool(
            "persist_raw_transcript",
            persist_raw_transcript,
        )
        observer_confidence_threshold = _strict_runtime_float(
            "observer_confidence_threshold",
            observer_confidence_threshold,
            minimum=0.0,
            maximum=1.0,
        )
        partial_plan_max_age_seconds = _strict_runtime_float(
            "partial_plan_max_age_seconds",
            partial_plan_max_age_seconds,
            minimum=0.0,
            maximum=60.0,
        )
        post_initial_action_wait_seconds = _strict_runtime_float(
            "post_initial_action_wait_seconds",
            post_initial_action_wait_seconds,
            minimum=0.0,
            maximum=5.0,
        )
        if deepseek_api_key is not None:
            deepseek_api_key = _strict_runtime_string(
                "deepseek_api_key",
                deepseek_api_key,
                maximum_length=8192,
            )
        if not isinstance(episode_dir, (str, Path)) or (
            isinstance(episode_dir, str) and not episode_dir.strip()
        ):
            raise ValueError("episode_dir must be a path")
        episode_id = (
            _new_episode_id() if episode_id is None else _strict_episode_id(episode_id)
        )
        if observer_mode not in {"off", "shadow", "active"}:
            raise ValueError("observer_mode must be one of: off, shadow, active")
        if observer_mode == "active" and observer_model == backend_model:
            raise ValueError(
                "Active realtime Observer model must differ from the Assessor model"
            )

        self.experiment_id = experiment_id
        self.user_id = user_id
        self.episode_dir = Path(episode_dir)
        self.backend_model = backend_model
        self.observer_model = observer_model
        self.deepseek_base_url = deepseek_base_url
        self.template = load_scid_template()
        self.ledger = AssessmentLedger(template=self.template)
        self.assessor = assessor or create_background_assessor(
            model=backend_model,
            prefer_deepseek=prefer_deepseek,
            api_key=deepseek_api_key,
            base_url=deepseek_base_url,
        )
        self.router = router or create_scid_interaction_router(
            model=backend_model,
            prefer_deepseek=prefer_deepseek,
        )
        self.control_router = RuleBasedSCIDInteractionRouter()
        self.observer = observer or create_incremental_observer(
            model=self.observer_model,
            prefer_deepseek=prefer_deepseek,
            api_key=deepseek_api_key,
            base_url=deepseek_base_url,
        )
        self.dialogue_model = dialogue_model or RuleBasedDialogueModel()
        self.safety_guard = SafetyGuard()
        self.observer_mode = observer_mode
        self.allow_one_step_speculation = allow_one_step_speculation
        self.persist_raw_transcript = persist_raw_transcript
        self.partial_plan_max_age_seconds = partial_plan_max_age_seconds
        self.post_initial_action_wait_seconds = post_initial_action_wait_seconds
        self.runtime_policy = runtime_policy or RuntimePolicy()
        retention = self.runtime_policy.retention
        self.fast_policy = FastForegroundPolicy()
        self.blackboard = ClinicalBlackboard(
            committed_state_version=self.ledger.state_version,
            current_field_id=self.ledger.current_field_id,
            observer_update_limit=retention.recent_observer_updates,
            partial_plan_history_limit=retention.partial_plan_history,
            candidate_evidence_limit=retention.candidate_evidence,
            contextual_memory_limit=retention.contextual_memories,
        )
        self.candidate_cache = CandidateUtteranceCache(
            self.dialogue_model,
            max_entries=retention.candidate_cache_entries,
        )
        self.latency_controller = ClinicalLatencyController(
            observer_confidence_threshold=observer_confidence_threshold
        )

        self.episode_id = episode_id
        self.event_store = EpisodeEventStore(
            episode_dir=self.episode_dir,
            episode_id=self.episode_id,
            queue_capacity=retention.event_queue_capacity,
        )
        self.session_actor = SessionActor(
            episode_id=self.episode_id,
            event_store=self.event_store,
            blackboard=self.blackboard,
            control_capacity=retention.control_mailbox_capacity,
            work_capacity=retention.work_mailbox_capacity,
        )
        self.speculation_saga = SpeculationSaga()
        self.model_gateway = ModelGateway()
        self.session_memory = SessionMemoryProjection()
        self.started_at = utc_now_iso()
        self.ended_at: str | None = None
        self.status = "in_progress"
        self.interaction_mode = "scid"
        self.pending_user_buffer = ""
        self.interaction_turns: list[SCIDInteractionTurn] = []
        self.latency_traces: dict[int, SCIDLatencyTrace] = {}
        self.runtime_turns: list[dict[str, Any]] = []
        self.foreground_actions: list[dict[str, Any]] = []

        self._started = False
        self._finished = False
        self._closed = False
        self._terminal_pending_status: str | None = None
        self._episode_path: Path | None = None
        self._latest_interaction_seq = 0
        self._sequence_states: dict[int, str] = {}
        self._generation_by_seq: dict[int, int] = {}
        self._delivery_receipts: dict[int, bool] = {}
        self._sequence_lock = asyncio.Lock()
        self._ledger_lock = asyncio.Lock()
        self._assessment_owners: dict[str, _AssessmentOwner] = {}
        self._assessment_owner_by_task: dict[asyncio.Task[Any], str] = {}
        self._write_lock = threading.Lock()
        self._observer_tasks: set[Any] = set()
        self._partial_observer_task: Any | None = None
        self._assessment_tasks: set[Any] = set()
        self._action_tasks: set[Any] = set()
        self._foreground_worker_tasks: set[Any] = set()
        self._speculative_watcher_tasks: set[asyncio.Task[None]] = set()
        self._pending_speculative_assessment: asyncio.Task[Any] | None = None
        self._turn_supervisors: dict[int, TurnSupervisor] = {}
        self._speculation_supervisor: TurnSupervisor | None = None
        self._speculation_spoken_event = asyncio.Event()
        self._actor_notification_tasks: set[asyncio.Task[Any]] = set()
        self._assessment_model_started: dict[int, asyncio.Event] = {}
        self._lifecycle_events: list[dict[str, Any]] = []

    @property
    def is_finished(self) -> bool:
        """Return whether the runtime has saved a terminal episode."""

        return self._finished

    @property
    def episode_path(self) -> Path | None:
        """Return the saved episode path, if any."""

        return self._episode_path

    def record_asr_partial(
        self,
        interaction_seq: int,
        *,
        timestamp: float | None = None,
    ) -> None:
        """Record the first ASR partial timestamp for one interaction."""

        _validate_interaction_seq(interaction_seq)
        if self._closed or self._finished:
            return
        recorded_at = _strict_optional_timestamp("timestamp", timestamp)
        trace = self._trace_for(interaction_seq)
        if trace.asr_partial_first_at is None:
            trace.asr_partial_first_at = recorded_at
            self._save_partial_snapshot()

    def record_asr_final(
        self,
        interaction_seq: int,
        *,
        timestamp: float | None = None,
    ) -> None:
        """Record an ASR final and invalidate work owned by earlier turns.

        Parameters
        ----------
        interaction_seq : int
            Sequence number reserved by the serving manager for the final text.
        timestamp : float | None, optional
            Wall-clock timestamp supplied by the ASR event.
        """

        _validate_interaction_seq(interaction_seq)
        if self._closed or self._finished:
            return
        generation = self.session_actor.advance_ingress_generation()
        self._generation_by_seq[interaction_seq] = generation
        if (
            len(self._generation_by_seq)
            > self.runtime_policy.retention.recent_latency_traces
        ):
            oldest = sorted(self._generation_by_seq)[
                : -self.runtime_policy.retention.recent_latency_traces
            ]
            for seq in oldest:
                self._generation_by_seq.pop(seq, None)
        recorded_at = _strict_optional_timestamp("timestamp", timestamp)
        if interaction_seq not in self._sequence_states:
            self._sequence_states[interaction_seq] = "reserved"
        self._latest_interaction_seq = max(
            self._latest_interaction_seq, interaction_seq
        )
        # A final transcript invalidates every older partial Observer generation,
        # including the case where the final is later rejected as ASR noise.
        self._trace_for(interaction_seq).asr_final_at = recorded_at

    def reject_asr_final(
        self,
        interaction_seq: int,
        *,
        timestamp: float | None = None,
        reason: str,
    ) -> None:
        """Advance sequence ownership for a rejected final without storing text."""

        if self._closed or self._finished:
            return
        reason = _strict_asr_rejection_reason(reason)
        self.record_asr_final(interaction_seq, timestamp=timestamp)
        self._sequence_states[interaction_seq] = "done"
        self._cancel_active_assessments()
        self._cancel_active_observers()
        self._record_interaction(
            interaction_seq,
            "",
            ignored_reason=reason,
        )
        self._trace_for(interaction_seq).error = reason
        self._save_partial_snapshot()

    def mark_first_segment_published(
        self,
        interaction_seq: int,
        *,
        timestamp: float | None = None,
    ) -> None:
        """Record when the initial runtime segment was published."""

        _validate_interaction_seq(interaction_seq)
        if self._closed or self._finished:
            return
        recorded_at = _strict_optional_timestamp("timestamp", timestamp)
        self._trace_for(interaction_seq).first_segment_published_at = recorded_at
        record = self._runtime_record_for(interaction_seq)
        if record is not None:
            record["response_delivery_status"] = "delivery_started"
        self._save_partial_snapshot()

    def mark_followup_segment_published(
        self,
        interaction_seq: int,
        *,
        timestamp: float | None = None,
    ) -> None:
        """Record when a bridge or selected action segment was published."""

        _validate_interaction_seq(interaction_seq)
        if self._closed or self._finished:
            return
        published_at = _strict_optional_timestamp("timestamp", timestamp)
        trace = self._trace_for(interaction_seq)
        trace.followup_segment_published_at = published_at
        record = self._runtime_record_for(interaction_seq)
        selected: dict[str, Any] | None = None
        if record is not None:
            selected_payload = record.get("selected_action")
            selected = selected_payload if isinstance(selected_payload, dict) else None
            if selected is None:
                record["bridge_delivery_status"] = "delivery_started"
                self._save_partial_snapshot()
                return
            record["action_delivery_status"] = "delivery_started"
            action_id = selected.get("action_id")
            for action in reversed(self.foreground_actions):
                if action.get("action_id") == action_id:
                    action["delivery_started"] = True
                    break
        elif not self.foreground_actions:
            self._save_partial_snapshot()
            return
        trace.speech_action_committed_at = published_at
        trace.action_delivery_status = "delivery_started"
        self._notify_actor(
            self._actor_mark_delivery_started(
                interaction_seq,
                selected=selected,
            )
        )
        self._save_partial_snapshot()

    async def _actor_mark_delivery_started(
        self,
        interaction_seq: int,
        *,
        selected: dict[str, Any] | None,
    ) -> None:
        """Record the first spoken action through the single-writer Actor."""

        def mutation() -> None:
            self.session_actor.transition_delivery_state(DeliveryPhase.STARTED)
            if selected is not None:
                self.blackboard.mark_spoken_action(selected)

        await self.session_actor.call(
            "mark_delivery_started",
            mutation,
            event_type="DeliveryStarted",
            event_payload={
                "action_id": (
                    selected.get("action_id") if selected is not None else None
                )
            },
            envelope=self._causal_envelope(interaction_seq),
        )

    def action_followup_was_published(self, interaction_seq: int) -> bool:
        """Return whether a scored turn emitted bridge or real-action audio."""

        _validate_interaction_seq(interaction_seq)
        trace = self.latency_traces.get(interaction_seq)
        return bool(trace and trace.followup_segment_published_at is not None)

    async def recover_action_stream_failure(
        self,
        interaction_seq: int,
        error: BaseException,
    ) -> AsyncIterator[str] | None:
        """Convert an action-worker failure into one auditable safe follow-up.

        The recovery never commits a score.  It preserves the user's answer in
        the pending buffer and asks a deterministic same-field clarification so
        a worker/supervisor failure cannot leave the audible turn half-finished.
        """

        _validate_interaction_seq(interaction_seq)
        if (
            self._closed
            or self._finished
            or not self.is_latest_interaction(interaction_seq)
        ):
            return None
        error_type = type(error).__name__
        trace = self._trace_for(interaction_seq)
        trace.error = f"action_stream_failure:{error_type}"
        record = self._runtime_record_for(interaction_seq)
        if record is None:
            return None
        record["action_stream_error_type"] = error_type

        selected_payload = record.get("selected_action")
        selected = selected_payload if isinstance(selected_payload, dict) else None
        recovered_existing_action = False
        recovery_text = ""
        if selected is not None:
            directive = selected.get("directive")
            if isinstance(directive, dict):
                question_text = directive.get("question_text")
                if isinstance(question_text, str) and question_text.strip():
                    recovery_text = question_text.strip()
                    recovered_existing_action = True

        if not recovery_text:
            turn = next(
                (
                    item
                    for item in reversed(self.interaction_turns)
                    if item.interaction_seq == interaction_seq
                ),
                None,
            )
            source_text = turn.user_text if turn is not None else ""
            if source_text:
                self.pending_user_buffer = self._combine_pending(
                    self.pending_user_buffer,
                    source_text,
                )
            recovery_text = self._action_failure_fallback_text(source_text)
            action = ForegroundAction(
                interaction_seq=interaction_seq,
                based_on_state_version=self.ledger.state_version,
                field_id=self.ledger.current_field_id,
                kind="clarify",
                source="runtime_failure_fallback",
                priority=100,
                directive=DialogueDirective(
                    directive_type="observer_probe",
                    field_id=self.ledger.current_field_id,
                    question_text=recovery_text,
                    instruction=("只朗读确定性故障恢复追问，不评分、不推进字段。"),
                    progress_text="action_stream_failure_recovery",
                    allowed_actions=["clarify"],
                ),
            )
            self._record_foreground_action(action)
            await self._actor_select_action(
                action,
                envelope=self._causal_envelope(interaction_seq),
            )
            record["selected_action"] = action.snapshot()
            record["action_delivery_status"] = "selected"
            record["selection_reason"] = "action_stream_failure_recovery"
            trace.foreground_action_ready_at = now_ts()
            trace.foreground_action_selected_at = trace.foreground_action_ready_at
            trace.foreground_action_source = action.source
            trace.foreground_action_kind = action.kind
            trace.action_delivery_status = "selected"

        await self.session_actor.emit(
            "OperationFailed",
            payload={
                "operation": "action_stream",
                "error_type": error_type,
                "recovered": True,
                "reused_selected_action": recovered_existing_action,
            },
            envelope=self._causal_envelope(interaction_seq),
        )
        self._save_partial_snapshot()
        return self._stream_text(
            recovery_text,
            interaction_seq=interaction_seq,
            phase="action",
            record=record,
            record_key="action_text",
        )

    def _action_failure_fallback_text(self, source_text: str) -> str:
        if self._is_uncertain_reply(source_text):
            return (
                "为了准确理解你刚才比较谨慎的回答，你的意思更接近明确没有，"
                "还是有些记不清？"
            )
        return (
            "刚才后台核对出现了短暂中断，你的回答已经保留。"
            "为了准确继续，你能再简短确认一下刚才的意思吗？"
        )

    def mark_interaction_stale(self, interaction_seq: int) -> None:
        """Record that an interaction produced a stale async result."""

        _validate_interaction_seq(interaction_seq)
        if self._closed or self._finished:
            return
        self._trace_for(interaction_seq).stale = True
        self._save_partial_snapshot()

    async def observe_asr_partial(
        self,
        user_text: str,
        *,
        interaction_seq: int,
    ) -> TurnInterpretation | None:
        """Observe a stable ASR partial without scoring or foreground output."""

        _validate_interaction_seq(interaction_seq)
        text = _normalize_input_text(user_text)
        if self.persist_raw_transcript and text:
            await self.event_store.append_artifact(
                interaction_seq=interaction_seq,
                role="user_partial",
                text=text,
            )
        if (
            self.observer_mode == "off"
            or not text
            or len(text) > _MAX_PARTIAL_TEXT_CHARS
            or self._finished
            or self._closed
        ):
            return None
        if interaction_seq < self.blackboard.latest_interaction_seq:
            return None
        await self.session_actor.call(
            "accept_partial_observation",
            lambda: (
                setattr(self.blackboard, "latest_interaction_seq", interaction_seq),
                setattr(self.blackboard, "stable_partial_text", text),
            ),
            lane=MailboxLane.WORK,
            event_type="PartialInputAccepted",
            event_payload={"character_count": len(text)},
            envelope=self._causal_envelope(interaction_seq),
            coalesce_key=f"partial-input:{interaction_seq}",
        )
        task = await self._start_observer_task(
            user_text=text,
            interaction_seq=interaction_seq,
            input_kind="partial",
            interaction_turn=None,
        )
        if task is None:
            return None
        return await task

    def reject_asr_partial(self, *, reason: str) -> None:
        """Invalidate a partial plan that was rejected at the serving edge."""

        if self._closed or self._finished:
            return
        reason = _strict_asr_rejection_reason(reason)
        self.session_actor.reject_partial_input(reason)
        self._save_partial_snapshot()

    async def aclose(self, *, status: str = "aborted") -> Path | None:
        """Invalidate, cancel, and boundedly drain all runtime-owned work."""

        if self._closed and self._episode_path is not None:
            return self._episode_path
        self._closed = True
        self._invalidate_all_assessment_owners()
        self._cancel_active_observers()
        tasks = set(self._observer_tasks)
        tasks.update(self._assessment_tasks)
        tasks.update(self._action_tasks)
        tasks.update(self._foreground_worker_tasks)
        tasks.update(self._speculative_watcher_tasks)
        if self._pending_speculative_assessment is not None:
            tasks.add(self._pending_speculative_assessment)
        await self._cancel_and_drain(tasks)
        async with self._ledger_lock:
            await self._discard_inactive_assessment_turns()
        self._assessment_owners.clear()
        self._assessment_owner_by_task.clear()
        async with self._sequence_lock:
            unfinished_sequences = [
                seq for seq, state in self._sequence_states.items() if state != "done"
            ]
            for seq in unfinished_sequences:
                self._sequence_states[seq] = "done"
                if self._terminal_pending_status is None:
                    self._trace_for(seq).stale = True
            if unfinished_sequences:
                self._lifecycle_events.append(
                    {
                        "event": "interaction_sequences_closed",
                        "count": len(unfinished_sequences),
                        "recorded_at": utc_now_iso(),
                    }
                )
                self._lifecycle_events = self._lifecycle_events[-32:]
        cache_close_task = asyncio.create_task(self.candidate_cache.aclose())
        cache_close_task.add_done_callback(self._consume_task_result)
        done, pending = await asyncio.wait(
            {cache_close_task},
            timeout=_TASK_DRAIN_TIMEOUT_SECONDS,
        )
        if done:
            with suppress(asyncio.CancelledError, Exception):
                cache_close_task.result()
        if pending:
            cache_close_task.cancel()
            self._record_lifecycle_timeout(
                event="candidate_cache_close_timeout",
                count=1,
            )
            logger.warning(
                "SCID candidate cache did not close within deadline - episode: %s",
                self.episode_id,
            )
        self._observer_tasks.clear()
        self._assessment_tasks.clear()
        self._action_tasks.clear()
        self._foreground_worker_tasks.clear()
        self._speculative_watcher_tasks.clear()
        self._pending_speculative_assessment = None
        notifications = list(self._actor_notification_tasks)
        if notifications:
            await asyncio.gather(*notifications, return_exceptions=True)
        self._actor_notification_tasks.clear()
        if self.speculation_saga.active and not self.session_actor.closed:
            await self._cancel_speculation(reason="runtime_shutdown")
        supervisors = list(self._turn_supervisors.values())
        self._turn_supervisors.clear()
        if self._speculation_supervisor is not None:
            supervisors.append(self._speculation_supervisor)
            self._speculation_supervisor = None
        if supervisors:
            await asyncio.gather(
                *(supervisor.close() for supervisor in supervisors),
                return_exceptions=True,
            )
        if self._started and not self._finished:
            final_status = self._terminal_pending_status or status
            if self._terminal_pending_status is not None:
                self._mark_terminal_delivery_failed()
            return await self._afinalize_episode(status=final_status)
        if self._started and not self.session_actor.closed:
            await self.session_actor.close(phase=SessionPhase.ABORTED)
        return self._episode_path

    def _mark_terminal_delivery_failed(self) -> None:
        """Record an undelivered terminal action before shutdown finalization."""

        if not self.runtime_turns:
            return
        record = self.runtime_turns[-1]
        if record.get("response_delivery_status") in {
            "pending",
            "delivery_started",
        }:
            record["response_delivery_status"] = "delivery_failed"
        if record.get("action_delivery_status") in {
            "selected",
            "delivery_started",
        }:
            record["action_delivery_status"] = "delivery_failed"
            seq = record.get("interaction_seq")
            if isinstance(seq, int):
                self._trace_for(seq).action_delivery_status = "delivery_failed"

    async def _cancel_and_drain(
        self,
        tasks: set[Any] | list[Any],
    ) -> None:
        owned = set(tasks)
        pending = {task for task in owned if not task.done()}
        for task in pending:
            task.cancel()
        if pending:
            waitables = {
                task.as_future() if isinstance(task, SupervisedTask) else task
                for task in pending
            }
            done, still_pending = await asyncio.wait(
                waitables,
                timeout=_TASK_DRAIN_TIMEOUT_SECONDS,
            )
            for task in done:
                with suppress(asyncio.CancelledError, Exception):
                    task.result()
            if still_pending:
                self._record_lifecycle_timeout(
                    event="task_cancellation_timeout",
                    count=len(still_pending),
                )
                logger.warning(
                    "SCID shutdown detached %s cancellation-resistant task(s) - episode: %s",
                    len(still_pending),
                    self.episode_id,
                )
                for task in still_pending:
                    task.add_done_callback(self._consume_task_result)
        for task in owned:
            if task.done():
                with suppress(asyncio.CancelledError, Exception):
                    task.result()

    def _record_lifecycle_timeout(self, *, event: str, count: int) -> None:
        if self._finished:
            return
        self._lifecycle_events.append(
            {
                "event": event,
                "count": count,
                "recorded_at": utc_now_iso(),
            }
        )
        self._lifecycle_events = self._lifecycle_events[-32:]

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        """Retrieve a detached task result without retaining its exception."""

        with suppress(asyncio.CancelledError, Exception):
            task.result()

    def _cancel_active_assessments(self) -> None:
        self._invalidate_all_assessment_owners()
        for owner in self._assessment_owners.values():
            self._trace_for(owner.interaction_seq).stale = True
            self._complete_interaction_seq(owner.interaction_seq)
        state = self.blackboard.speculative_advance
        if state is not None:
            self._trace_for(state.source_interaction_seq).speculative_cancelled = True
        owned_tasks = set(self._assessment_tasks)
        owned_tasks.update(self._assessment_owner_by_task)
        for task in owned_tasks:
            if not task.done():
                task.cancel()

    def _cancel_active_observers(self) -> None:
        """Invalidate and cancel Observer work for a terminal interruption."""

        self.session_actor.invalidate_observer_generation()
        for task in list(self._observer_tasks):
            if not task.done():
                task.cancel()

    async def _invalidate_superseded_assessments(
        self,
        interaction_seq: int,
    ) -> None:
        """Invalidate older assessment owners before a new turn can score.

        A one-step speculative assessment is the sole exception: it owns the
        preceding field transaction and must be allowed to finish so the new
        reply can either be promoted or repaired. Every other older owner is
        made stale before its task is cancelled, so a model that swallows
        cancellation still cannot commit.
        """

        if self._closed or self._finished:
            return
        speculative_task = self._pending_speculative_assessment
        speculative_state = self.blackboard.speculative_advance
        cancelled: set[asyncio.Task[Any]] = set()
        superseded_sequences: set[int] = set()
        for task, token in list(self._assessment_owner_by_task.items()):
            owner = self._assessment_owners.get(token)
            if (
                owner is None
                or owner.interaction_seq >= interaction_seq
                or task is speculative_task
                or (
                    speculative_task is not None
                    and speculative_state is not None
                    and owner.interaction_seq
                    == speculative_state.source_interaction_seq
                )
            ):
                continue
            owner.active = False
            superseded_sequences.add(owner.interaction_seq)
            if not task.done():
                task.cancel()
                cancelled.add(task)

        async with self._ledger_lock:
            await self._discard_inactive_assessment_turns()

        for seq in superseded_sequences:
            self._trace_for(seq).stale = True
            self._complete_interaction_seq(seq)
        if cancelled:
            await self._cancel_and_drain(cancelled)

    def _invalidate_assessment_owner(self, token: str) -> None:
        owner = self._assessment_owners.get(token)
        if owner is not None:
            owner.active = False

    def _invalidate_all_assessment_owners(self) -> None:
        for owner in self._assessment_owners.values():
            owner.active = False

    def _invalidate_assessment_task(
        self,
        task: asyncio.Task[Any] | None,
    ) -> None:
        if task is None:
            return
        token = self._assessment_owner_by_task.get(task)
        if token is not None:
            self._invalidate_assessment_owner(token)

    def _release_assessment_owner(
        self,
        owner: _AssessmentOwner,
        task: asyncio.Task[Any] | None,
    ) -> None:
        """Remove all bookkeeping for an assessment that can no longer commit."""

        owner.active = False
        self._assessment_owners.pop(owner.token, None)
        if task is not None:
            self._assessment_owner_by_task.pop(task, None)

    async def _discard_inactive_assessment_turns(self) -> int:
        """Discard only latest uncommitted turns owned by invalid work."""

        def mutation() -> int:
            discarded = 0
            while self.ledger.turns:
                turn = self.ledger.turns[-1]
                if turn.decision is not None:
                    return discarded
                owner = next(
                    (
                        candidate
                        for candidate in self._assessment_owners.values()
                        if candidate.turn_id == turn.turn_id
                    ),
                    None,
                )
                if owner is None or owner.active:
                    return discarded
                if not self.ledger.discard_turn_if_uncommitted(turn.turn_id):
                    return discarded
                discarded += 1
                self._assessment_owners.pop(owner.token, None)
                for task, token in list(self._assessment_owner_by_task.items()):
                    if token == owner.token:
                        self._assessment_owner_by_task.pop(task, None)
            return discarded

        return await self.session_actor.call(
            "discard_inactive_assessment_turns",
            mutation,
            event_type="AssessmentTurnsDiscarded",
            event_payload={},
        )

    async def _actor_discard_turn(self, turn_id: int) -> bool:
        return await self.session_actor.call(
            "discard_assessment_turn",
            lambda: self.ledger.discard_turn_if_uncommitted(turn_id),
            event_type="AssessmentTurnDiscarded",
            event_payload={"turn_id": turn_id},
        )

    async def _actor_discard_interaction_turn(
        self,
        interaction_seq: int,
    ) -> bool:
        """Discard an uncommitted Ledger reservation by interaction identity."""

        def mutation() -> bool:
            if not self.ledger.turns:
                return False
            turn = self.ledger.turns[-1]
            if turn.interaction_seq != interaction_seq or turn.decision is not None:
                return False
            return self.ledger.discard_turn_if_uncommitted(turn.turn_id)

        return await self.session_actor.call(
            "discard_assessment_interaction",
            mutation,
            event_type="AssessmentTurnDiscarded",
            event_payload={"interaction_seq": interaction_seq},
        )

    async def _actor_begin_turn(
        self,
        *,
        user_text: str,
        interaction_seq: int,
        envelope: CausalEnvelope,
    ):
        def mutation():
            turn = self.ledger.begin_turn(
                user_text,
                interaction_seq=interaction_seq,
            )
            self.session_actor.transition_assessment_state(
                AssessmentPhase.RESERVED,
                envelope=envelope,
            )
            return turn

        return await self.session_actor.call(
            "begin_assessment_turn",
            mutation,
            event_type="AssessmentReserved",
            event_payload={"field_id": self.ledger.current_field_id},
            envelope=envelope,
        )

    async def _actor_apply_decision(
        self,
        *,
        decision: Any,
        turn_id: int,
        expected_field_id: str | None,
        expected_state_version: int,
        envelope: CausalEnvelope,
    ) -> None:
        state_before = {
            "field_id": self.ledger.current_field_id,
            "state_version": self.ledger.state_version,
            "pending_clarification": bool(self.ledger.pending_clarification),
        }

        def mutation() -> None:
            self.ledger.apply_decision(
                decision,
                turn_id=turn_id,
                expected_field_id=expected_field_id,
                expected_state_version=expected_state_version,
            )
            self.blackboard.sync_committed_state(
                state_version=self.ledger.state_version,
                current_field_id=self.ledger.current_field_id,
            )
            self.session_memory.observe_commit(
                field_id=expected_field_id,
                score=decision.score,
                action=decision.next_action,
                confidence=decision.confidence,
                state_version=self.ledger.state_version,
            )

        await self.session_actor.call(
            "apply_assessment_decision",
            mutation,
            event_type="AssessmentCommitted",
            event_payload={
                "turn_id": turn_id,
                "field_id": expected_field_id,
                "action": decision.next_action,
                "score": decision.score,
            },
            envelope=envelope,
        )
        await self.session_actor.emit(
            "StateDiffRecorded",
            payload={
                "operation": "apply_assessment_decision",
                "before": state_before,
                "decision": {
                    "action": decision.next_action,
                    "score": decision.score,
                    "confidence": decision.confidence,
                },
                "after": {
                    "field_id": self.ledger.current_field_id,
                    "state_version": self.ledger.state_version,
                    "pending_clarification": bool(self.ledger.pending_clarification),
                },
            },
            envelope=envelope,
        )

    async def _actor_set_assessor_status(
        self,
        status: str,
        *,
        envelope: CausalEnvelope,
        field_id: str | None = None,
    ) -> None:
        """Update the Blackboard assessor projection through the Actor."""

        def mutation() -> bool:
            if not self.session_actor.assessment_envelope_is_current(envelope):
                return False
            self.blackboard.assessor_status = status
            if field_id is not None or status == "idle":
                self.blackboard.assessor_field_id = field_id
            return True

        await self.session_actor.call(
            "set_assessor_status",
            mutation,
            event_type="AssessorStatusChanged",
            event_payload={"status": status, "field_id": field_id},
            envelope=envelope,
            event_predicate=bool,
        )

    async def _actor_clear_foreground_probe(
        self,
        *,
        interaction_seq: int,
        reason: str,
    ) -> None:
        """Clear provisional probe state through the single writer."""

        await self.session_actor.call(
            "clear_foreground_probe",
            self.blackboard.clear_foreground_probe,
            event_type="ForegroundProbeCleared",
            event_payload={"reason": reason},
            envelope=self._causal_envelope(interaction_seq),
        )

    async def _actor_archive_ledger_turns(self) -> int:
        """Archive committed Ledger turns through the single-writer Actor."""

        return await self.session_actor.call(
            "archive_committed_ledger_turns",
            lambda: self.ledger.archive_committed_turns(
                max_recent=self.runtime_policy.retention.recent_ledger_turns
            ),
            event_type="LedgerTurnsArchived",
            event_payload={
                "max_recent": self.runtime_policy.retention.recent_ledger_turns
            },
        )

    async def _actor_select_action(
        self,
        action: ForegroundAction,
        *,
        envelope: CausalEnvelope,
    ) -> None:
        """Publish one immutable selected action and delivery phase."""

        payload = action.snapshot()

        def mutation() -> None:
            self.blackboard.mark_selected_action(payload)
            self.session_actor.transition_delivery_state(DeliveryPhase.SELECTED)

        await self.session_actor.call(
            "select_foreground_action",
            mutation,
            event_type="ActionSelected",
            event_payload={
                "action_id": action.action_id,
                "kind": action.kind,
                "source": action.source,
                "speculative": action.speculative,
            },
            envelope=envelope,
        )

    async def start(self) -> str:
        """Render the product-boundary disclosure and first frontend turn."""

        if self._started:
            text = await RuleBasedDialogueModel().render(self.ledger.get_directive())
            return render_session_opening(text)
        await self.session_actor.start()
        self._started = True
        # Product-boundary text and its first structured question are
        # deterministic; an external dialogue model cannot rewrite them.
        text = await RuleBasedDialogueModel().render(self.ledger.get_directive())
        self._save_partial_snapshot()
        return render_session_opening(text)

    async def _run_model_call(
        self,
        operation: Any,
        *,
        component: str,
        purpose: str,
        priority: ModelPriority,
        envelope: CausalEnvelope,
    ) -> Any:
        """Execute and persist a redacted, request-level model-call trace."""

        queued_at = monotonic_ts()
        try:
            result = await self.model_gateway.run(
                operation,
                priority=priority,
                deadline_monotonic=(envelope.deadline_monotonic or monotonic_ts()),
                request_id=envelope.request_id,
            )
        except asyncio.CancelledError:
            if not self.session_actor.closed:
                await self.session_actor.emit(
                    "ModelCallCancelled",
                    payload={
                        "component": component,
                        "purpose": purpose,
                        "priority": priority.name,
                        "status": "cancelled",
                        "total_latency_ms": round(
                            (monotonic_ts() - queued_at) * 1000, 3
                        ),
                    },
                    envelope=envelope,
                )
            raise
        except Exception as exc:
            if not self.session_actor.closed:
                await self.session_actor.emit(
                    "ModelCallFailed",
                    payload={
                        "component": component,
                        "purpose": purpose,
                        "priority": priority.name,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "total_latency_ms": round(
                            (monotonic_ts() - queued_at) * 1000, 3
                        ),
                    },
                    envelope=envelope,
                )
            raise
        if not self.session_actor.closed:
            await self.session_actor.emit(
                "ModelCallCompleted",
                payload={
                    "component": component,
                    "purpose": purpose,
                    "priority": priority.name,
                    "status": "completed",
                    "total_latency_ms": round((monotonic_ts() - queued_at) * 1000, 3),
                    "result_summary": _audit_model_result_summary(result),
                },
                envelope=envelope,
            )
        return result

    def _should_defer_for_speculation(self) -> bool:
        state = self.blackboard.speculative_advance
        if state is None:
            return False
        repair = self.blackboard.repair_pending or {}
        if state.status == "repair_pending":
            return not bool(repair.get("announced"))
        return (
            state.status == "pending"
            and self.ledger.current_field_id == state.source_field_id
        )

    async def _promote_confirmed_speculative_reply(self) -> None:
        """Move a deferred speculative reply into normal current-field input."""

        state = self.blackboard.speculative_advance
        if (
            state is None
            or state.status != "confirmed"
            or self.ledger.current_field_id != state.speculative_field_id
        ):
            return
        if state.deferred_user_text:
            self.pending_user_buffer = self._combine_pending(
                self.pending_user_buffer,
                state.deferred_user_text,
            )
        await self._transition_speculation(
            SpeculationPhase.CLOSED,
            reason="confirmed_reply_promoted",
            mutation=self.blackboard.complete_speculation,
        )
        logger.info(
            "SCID confirmed speculation promoted - episode: %s, field: %s",
            self.episode_id,
            self.ledger.current_field_id,
        )

    async def _resolve_speculative_reply(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
        record: dict[str, Any],
    ) -> str:
        task = self._pending_speculative_assessment
        if task is not None and not task.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(task)

        if not self.is_latest_interaction(turn.interaction_seq):
            turn.stale = True
            self._trace_for(turn.interaction_seq).stale = True
            return ""

        state = self.blackboard.speculative_advance
        if (
            state is not None
            and self.ledger.current_field_id == state.source_field_id
            and self.ledger.pending_clarification
            and self.blackboard.repair_pending is None
        ):
            repair = RepairRequest(
                source_field_id=state.source_field_id,
                speculative_field_id=state.speculative_field_id,
                reason=self.ledger.pending_clarification,
                required_slot="criterion_evidence",
                suggested_action="clarify",
            ).snapshot()
            if self.speculation_saga.phase is SpeculationPhase.PENDING_COMMIT:
                await self._transition_speculation(
                    SpeculationPhase.COMPENSATING,
                    reason="deferred_reply_observed_repair",
                    mutation=lambda: self.blackboard.request_repair(repair),
                )
            else:
                await self.session_actor.call(
                    "request_speculation_repair",
                    lambda: self.blackboard.request_repair(repair),
                    event_type="SpeculationRepairRequested",
                    event_payload={"reason": "pending_clarification"},
                    envelope=self._causal_envelope(turn.interaction_seq),
                )
        repair_payload = self.blackboard.repair_pending
        if repair_payload is not None:
            if self.speculation_saga.phase is SpeculationPhase.PENDING_COMMIT:
                await self._transition_speculation(
                    SpeculationPhase.COMPENSATING,
                    reason="deferred_reply_observed_repair",
                )
            request = RepairRequest(
                source_field_id=str(
                    repair_payload.get("source_field_id")
                    or self.ledger.current_field_id
                    or ""
                ),
                speculative_field_id=repair_payload.get("speculative_field_id"),
                reason=str(repair_payload.get("reason") or "信息仍需确认"),
                required_slot=str(
                    repair_payload.get("required_slot") or "criterion_evidence"
                ),
                suggested_action=str(
                    repair_payload.get("suggested_action") or "clarify"
                ),
            )
            await self.session_actor.call(
                "mark_speculation_repair_announced",
                self._mark_repair_announced,
                event_type="SpeculationRepairAnnounced",
                event_payload={},
                envelope=self._causal_envelope(turn.interaction_seq),
            )
            directive = build_repair_directive(
                request,
                clarification_question=self.ledger.pending_clarification or "",
            )
            text = await self.dialogue_model.render(directive)
            record["repair_required"] = True
            self._save_partial_snapshot()
            return text

        if (
            state is not None
            and state.status in {"pending", "confirmed"}
            and self.ledger.current_field_id == state.speculative_field_id
            and self.ledger.pending_clarification is None
        ):
            deferred_text = state.deferred_user_text or turn.user_text
            if self.speculation_saga.phase is SpeculationPhase.PENDING_COMMIT:
                await self._transition_speculation(
                    SpeculationPhase.CONFIRMED,
                    reason="deferred_reply_observed_commit",
                    mutation=self.blackboard.complete_speculation,
                )
            else:
                await self.session_actor.call(
                    "complete_speculation_state",
                    self.blackboard.complete_speculation,
                    event_type="SpeculationStateCompleted",
                    event_payload={},
                    envelope=self._causal_envelope(turn.interaction_seq),
                )
            await self._transition_speculation(
                SpeculationPhase.CLOSED,
                reason="deferred_reply_promoted",
            )
            scoring_route = SCIDRouteDecision(
                route="scid_answer",
                confidence=route.confidence,
                should_score=True,
                normalized_user_text=deferred_text,
                safe_frontend_content="",
                reasoning_summary=("上一扫描字段已提交，处理暂存的下一字段回答。"),
                raw_payload={"deferred_speculative_reply": True},
            )
            return await self._score_scid_answer(
                turn=turn,
                route=scoring_route,
            )

        record["repair_required"] = True
        directive = DialogueDirective(
            directive_type="repair_prompt",
            field_id=self.ledger.current_field_id,
            question_text=(
                self.ledger.pending_clarification
                or "我回到刚才那一点再确认一下，你能再具体说说吗？"
            ),
            instruction="自然回到当前问题，不要暴露内部状态。",
        )
        return await self.dialogue_model.render(directive)

    def _mark_repair_announced(self) -> None:
        """Mark the current repair request as presented to the user."""

        if self.blackboard.repair_pending is not None:
            self.blackboard.repair_pending["announced"] = True

    async def _watch_speculative_assessment(
        self,
        *,
        assessment_task: asyncio.Task[Any] | SupervisedTask[Any],
        source_field_id: str,
        speculative_field_id: str,
        interaction_seq: int,
    ) -> None:
        try:
            await asyncio.shield(assessment_task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "SCID speculative assessment watcher failed - seq: %s, error: %s",
                interaction_seq,
                exc,
            )
        finally:
            if self._pending_speculative_assessment is assessment_task:
                self._pending_speculative_assessment = None
            supervisor = self._speculation_supervisor
            self._speculation_supervisor = None
            if supervisor is not None:
                await supervisor.close()

        if self.speculation_saga.phase in {
            SpeculationPhase.PROPOSED,
            SpeculationPhase.SELECTED,
            SpeculationPhase.SPOKEN,
        }:
            await self._speculation_spoken_event.wait()

        state = self.blackboard.speculative_advance
        if state is None or state.source_interaction_seq != interaction_seq:
            return
        if self.interaction_mode in {"crisis", "completed"} or self._finished:
            self._trace_for(interaction_seq).speculative_cancelled = True
            await self._cancel_speculation(reason="terminal_preemption")
            self._save_partial_snapshot()
            return
        if (
            self.ledger.current_field_id == speculative_field_id
            and self.ledger.pending_clarification is None
        ):
            await self._transition_speculation(
                SpeculationPhase.CONFIRMED,
                reason="assessment_confirmed_advance",
                mutation=self.blackboard.confirm_speculation,
            )
            logger.info(
                "SCID speculative advance confirmed - seq: %s, field: %s",
                interaction_seq,
                speculative_field_id,
            )
        else:
            self._trace_for(interaction_seq).speculative_cancelled = True
            repair = RepairRequest(
                source_field_id=source_field_id,
                speculative_field_id=speculative_field_id,
                reason=(
                    self.ledger.pending_clarification or "后台未确认上一字段可以推进"
                ),
                required_slot="criterion_evidence",
                suggested_action="clarify",
            ).snapshot()
            await self._transition_speculation(
                SpeculationPhase.COMPENSATING,
                reason="assessment_rejected_advance",
                mutation=lambda: self.blackboard.request_repair(repair),
            )
            logger.info(
                "SCID speculative advance requires repair - seq: %s, field: %s",
                interaction_seq,
                source_field_id,
            )
        self._save_partial_snapshot()

    async def _begin_speculation(
        self,
        *,
        action: ForegroundAction,
        source_field_id: str,
        speculative_field_id: str,
        source_state_version: int,
    ) -> None:
        """Create and select the sole speculative Saga through the Actor."""

        envelope = self._causal_envelope(
            action.interaction_seq,
            field_id=source_field_id,
            state_version=source_state_version,
        )

        def begin() -> int:
            self.blackboard.begin_speculation(
                source_field_id=source_field_id,
                speculative_field_id=speculative_field_id,
                source_interaction_seq=action.interaction_seq,
                based_on_state_version=source_state_version,
                question_text=action.directive.question_text,
            )
            version = self.speculation_saga.begin(
                action_id=action.action_id,
                source_interaction_seq=action.interaction_seq,
                source_field_id=source_field_id,
                speculative_field_id=speculative_field_id,
            )
            self.session_actor.state.speculation_phase = SpeculationPhase.PROPOSED
            return version

        self._speculation_spoken_event.clear()
        await self.session_actor.call(
            "begin_speculation_saga",
            begin,
            event_type="SagaTransitioned",
            event_payload={
                "phase": SpeculationPhase.PROPOSED.value,
                "reason": "candidate_created",
                "action_id": action.action_id,
            },
            envelope=envelope,
        )
        await self._transition_speculation(
            SpeculationPhase.SELECTED,
            reason="action_selected",
            action_id=action.action_id,
            envelope=envelope,
        )

    async def _transition_speculation(
        self,
        phase: SpeculationPhase,
        *,
        reason: str,
        action_id: str | None = None,
        mutation: Any | None = None,
        envelope: CausalEnvelope | None = None,
    ) -> int:
        """Apply one idempotent Saga transition and optional compensation."""

        def transition() -> int:
            if (
                self.speculation_saga.phase is SpeculationPhase.IDLE
                and phase is SpeculationPhase.CLOSED
            ):
                if mutation is not None:
                    mutation()
                return self.speculation_saga.transition_version
            should_transition = self.speculation_saga.validate_transition(
                phase,
                action_id=action_id,
            )
            if should_transition and mutation is not None:
                mutation()
            version = self.speculation_saga.transition(
                phase,
                reason=reason,
                action_id=action_id,
            )
            self.session_actor.state.speculation_phase = phase
            return version

        return await self.session_actor.call(
            "transition_speculation_saga",
            transition,
            event_type="SagaTransitioned",
            event_payload={
                "phase": phase.value,
                "reason": reason,
                "action_id": action_id or self.speculation_saga.action_id,
            },
            envelope=envelope,
        )

    async def _ensure_speculation_spoken(self, interaction_seq: int) -> None:
        """Advance a selected speculative action to pending commit once."""

        if (
            self.speculation_saga.source_interaction_seq != interaction_seq
            or self.speculation_saga.phase
            not in {SpeculationPhase.SELECTED, SpeculationPhase.SPOKEN}
        ):
            return
        envelope = self._causal_envelope(interaction_seq)
        if self.speculation_saga.phase is SpeculationPhase.SELECTED:
            await self._transition_speculation(
                SpeculationPhase.SPOKEN,
                reason="first_delivery_segment",
                envelope=envelope,
            )
        if self.speculation_saga.phase is SpeculationPhase.SPOKEN:
            await self._transition_speculation(
                SpeculationPhase.PENDING_COMMIT,
                reason="awaiting_assessment_commit",
                envelope=envelope,
            )
        if self.speculation_saga.phase is SpeculationPhase.PENDING_COMMIT:
            self._speculation_spoken_event.set()

    async def _cancel_speculation(self, *, reason: str) -> None:
        """Compensate and close the active Saga without duplicate repair."""

        if not self.speculation_saga.active:
            await self.session_actor.call(
                "clear_inactive_speculation",
                self.blackboard.complete_speculation,
                event_type="SpeculationStateCompleted",
                event_payload={"reason": reason},
            )
            self._speculation_spoken_event.set()
            return
        if self.speculation_saga.phase is not SpeculationPhase.CANCELLED:
            await self._transition_speculation(
                SpeculationPhase.CANCELLED,
                reason=reason,
                mutation=self.blackboard.complete_speculation,
            )
        await self._transition_speculation(
            SpeculationPhase.CLOSED,
            reason=f"{reason}_closed",
        )
        self._speculation_spoken_event.set()

    async def _start_observer_task(
        self,
        *,
        user_text: str,
        interaction_seq: int,
        input_kind: str,
        interaction_turn: SCIDInteractionTurn | None,
        supervisor: TurnSupervisor | None = None,
    ) -> asyncio.Task[TurnInterpretation] | SupervisedTask[TurnInterpretation] | None:
        if self.observer_mode == "off":
            return None
        if input_kind == "partial" and self._partial_observer_task is not None:
            if not self._partial_observer_task.done():
                self._partial_observer_task.cancel()
        observer_version = await self.session_actor.call(
            "reserve_observer_version",
            self.blackboard.next_observer_version,
            lane=(MailboxLane.WORK if input_kind == "partial" else MailboxLane.CONTROL),
            event_type="ObserverReserved",
            event_payload={"input_kind": input_kind},
            envelope=self._causal_envelope(interaction_seq),
        )
        context = self._build_observer_context(
            user_text=user_text,
            interaction_seq=interaction_seq,
            observer_version=observer_version,
            input_kind=input_kind,
        )
        observer_work = self._run_observer(
            context=context,
            interaction_turn=interaction_turn,
        )
        task = (
            supervisor.spawn(
                observer_work,
                name=f"scid-final-observer-{interaction_seq}",
            )
            if supervisor is not None
            else asyncio.create_task(observer_work)
        )
        self._observer_tasks.add(task)
        task.add_done_callback(self._observer_tasks.discard)
        if input_kind == "partial":
            self._partial_observer_task = task
            task.add_done_callback(self._clear_partial_observer_task)
        return task

    def _clear_partial_observer_task(self, task: Any) -> None:
        """Forget the latest partial Observer after it terminates."""

        if self._partial_observer_task is task:
            self._partial_observer_task = None

    async def _run_observer(
        self,
        *,
        context: dict[str, Any],
        interaction_turn: SCIDInteractionTurn | None,
    ) -> TurnInterpretation:
        seq = int(context["interaction_seq"])
        trace = self._trace_for(seq)
        if trace.observer_started_at is None:
            trace.observer_started_at = now_ts()
        observer_envelope = self._causal_envelope(seq)
        interpretation = await self._run_model_call(
            lambda: self.observer.observe(context=context),
            component="observer",
            purpose=(
                "partial_interpretation"
                if context.get("input_kind") == "partial"
                else "final_interpretation"
            ),
            priority=(
                ModelPriority.PARTIAL_OBSERVER
                if context.get("input_kind") == "partial"
                else ModelPriority.FINAL_OBSERVER
            ),
            envelope=observer_envelope,
        )
        if self._closed or self._finished:
            interpretation.stale = True
            return interpretation
        try:
            interpretation = validate_observer_provenance(
                interpretation,
                interaction_seq=seq,
                observer_version=int(context["observer_version"]),
                state_version=int(context["state_version"]),
                field_id=context.get("current_field_id"),
                module_id=(context.get("current_field") or {}).get("module"),
                user_text=str(context.get("user_text") or ""),
                input_kind=str(context["input_kind"]),
            )
        except ObserverParseError as exc:
            logger.warning(
                "SCID observer provenance rejected - episode: %s, seq: %s, error: %s",
                self.episode_id,
                seq,
                exc,
            )
            interpretation = TurnInterpretation(
                interaction_seq=seq,
                observer_version=int(context["observer_version"]),
                based_on_state_version=int(context["state_version"]),
                field_id=context.get("current_field_id"),
                dialogue_acts=[],
                current_field_relevance=0.0,
                related_field_ids=[],
                related_module_ids=[],
                contextual_memories=[],
                evidence_candidates=[],
                recommended_action="hold_for_assessor",
                missing_slots=[],
                needs_deep_assessment=True,
                commit_required=True,
                confidence=0.0,
                input_kind=str(context["input_kind"]),
                source="provenance_rejected",
                raw_payload={"rejected": type(exc).__name__},
            )
        current = await self.session_actor.call(
            "apply_observer_result",
            lambda: self.blackboard.apply_observation(interpretation),
            lane=(
                MailboxLane.WORK
                if interpretation.input_kind == "partial"
                else MailboxLane.CONTROL
            ),
            event_type="ObserverUpdated",
            event_payload={
                "input_kind": interpretation.input_kind,
                "recommended_action": interpretation.recommended_action,
                "stale": interpretation.stale,
            },
            envelope=self._causal_envelope(seq),
            coalesce_key=(
                f"partial-observer:{seq}"
                if interpretation.input_kind == "partial"
                else None
            ),
        )
        if interaction_turn is not None:
            interaction_turn.observer_decision = (
                interpretation.snapshot()
                if current
                else {
                    "interaction_seq": interpretation.interaction_seq,
                    "observer_version": interpretation.observer_version,
                    "based_on_state_version": interpretation.based_on_state_version,
                    "field_id": interpretation.field_id,
                    "input_kind": interpretation.input_kind,
                    "stale": True,
                    "status": "discarded_stale",
                }
            )
        trace.observer_action_ready_at = now_ts()
        trace.observer_action = interpretation.recommended_action
        trace.observer_confidence = interpretation.confidence
        trace.observer_stale = not current
        if trace.assessor_action is not None:
            trace.observer_assessor_agree = self._observer_assessor_agree(
                interpretation.recommended_action,
                trace.assessor_action,
            )
        if (
            current
            and interpretation.input_kind == "partial"
            and self.observer_mode == "active"
        ):
            await self._prepare_partial_observer_plan(
                interpretation=interpretation,
                partial_text=str(context.get("user_text") or ""),
            )
        logger.info(
            "SCID observer recorded - seq: %s, action: %s, current: %s",
            seq,
            interpretation.recommended_action,
            current,
        )
        self._save_partial_snapshot()
        return interpretation

    async def _prepare_partial_observer_plan(
        self,
        *,
        interpretation: TurnInterpretation,
        partial_text: str,
    ) -> None:
        """Cache one safe action proposed from stable partial ASR text."""

        field = self.ledger.current_field
        if (
            field is None
            or interpretation.field_id != field.field_id
            or interpretation.based_on_state_version != self.ledger.state_version
        ):
            return
        directive = self._observer_candidate_directive(
            interpretation=interpretation,
            field=field,
        )
        ready_at = now_ts()
        plan = PartialObserverPlan(
            interaction_seq=interpretation.interaction_seq,
            field_id=field.field_id,
            based_on_state_version=self.ledger.state_version,
            observer_version=interpretation.observer_version,
            partial_text=partial_text.strip(),
            interpretation=interpretation,
            ready_at=ready_at,
            ready_monotonic=monotonic_ts(),
        )
        await self.session_actor.call(
            "prepare_partial_observer_plan",
            lambda: self.blackboard.set_partial_plan(plan),
            lane=MailboxLane.WORK,
            event_type="PartialPlanPrepared",
            event_payload={"field_id": field.field_id},
            envelope=self._causal_envelope(interpretation.interaction_seq),
            coalesce_key=f"partial-plan:{interpretation.interaction_seq}",
        )
        self._trace_for(interpretation.interaction_seq).partial_plan_ready_at = ready_at
        self._save_partial_snapshot()
        if directive is None:
            return
        candidate_texts = await self._generate_candidates(
            interaction_seq=interpretation.interaction_seq,
            field_id=field.field_id,
            state_version=interpretation.based_on_state_version,
            directives={interpretation.recommended_action: directive},
        )
        applied = await self.session_actor.call(
            "attach_partial_plan_candidates",
            lambda: self._attach_partial_plan_candidates(plan, candidate_texts),
            lane=MailboxLane.WORK,
            event_type="PartialPlanCandidatesAttached",
            event_payload={"candidate_count": len(candidate_texts)},
            envelope=self._causal_envelope(interpretation.interaction_seq),
            coalesce_key=f"partial-plan-candidates:{interpretation.interaction_seq}",
        )
        if applied:
            self._save_partial_snapshot()

    def _attach_partial_plan_candidates(
        self,
        plan: PartialObserverPlan,
        candidate_texts: dict[str, str],
    ) -> bool:
        """Attach generated text only while the partial plan is still current."""

        if self.blackboard.latest_partial_plan is not plan:
            return False
        plan.candidate_texts.update(candidate_texts)
        return True

    async def _promote_partial_plan_for_final(
        self,
        *,
        user_text: str,
        interaction_seq: int,
    ) -> PartialObserverPlan | None:
        """Promote a matching partial plan for final-turn arbitration."""

        def mutation() -> tuple[PartialObserverPlan | None, str | None, float | None]:
            plan = self.blackboard.latest_partial_plan
            if plan is None:
                return None, None, None
            reason = self._partial_plan_rejection_reason(
                plan=plan,
                final_text=user_text,
                interaction_seq=interaction_seq,
            )
            if reason:
                self.blackboard.reject_partial_plan(reason)
                return None, reason, None
            promoted_at = now_ts()
            promoted = self.blackboard.promote_partial_plan(promoted_at=promoted_at)
            return promoted, None, promoted_at

        promoted, reason, promoted_at = await self.session_actor.call(
            "promote_partial_plan",
            mutation,
            lane=MailboxLane.CONTROL,
            event_type="PartialPlanResolved",
            event_payload={"interaction_seq": interaction_seq},
            envelope=self._causal_envelope(interaction_seq),
        )
        trace = self._trace_for(interaction_seq)
        if reason:
            trace.partial_plan_rejected_reason = reason
            return None
        if promoted_at is not None:
            trace.partial_plan_promoted_at = promoted_at
        return promoted

    async def _actor_begin_blackboard_interaction(
        self,
        *,
        interaction_seq: int,
        user_text: str,
        reject_partial_reason: str | None = None,
    ) -> None:
        """Atomically resolve partial state and accept the final interaction."""

        def mutation() -> None:
            if reject_partial_reason is not None:
                self.blackboard.reject_partial_plan(reject_partial_reason)
            self.blackboard.stable_partial_text = ""
            self.blackboard.begin_interaction(interaction_seq, user_text)

        await self.session_actor.call(
            "begin_blackboard_interaction",
            mutation,
            event_type="BlackboardInteractionStarted",
            event_payload={
                "partial_rejection_reason": reject_partial_reason,
            },
            envelope=self._causal_envelope(interaction_seq),
        )

    def _partial_plan_rejection_reason(
        self,
        *,
        plan: PartialObserverPlan,
        final_text: str,
        interaction_seq: int,
    ) -> str:
        if plan.interaction_seq != interaction_seq:
            return "interaction_seq_mismatch"
        if plan.field_id != self.ledger.current_field_id:
            return "field_mismatch"
        if plan.based_on_state_version != self.ledger.state_version:
            return "state_version_mismatch"
        ready_monotonic = plan.ready_monotonic
        if ready_monotonic is None:
            # Compatibility for in-memory plans created before schema v3.
            expired = now_ts() - plan.ready_at > self.partial_plan_max_age_seconds
        else:
            expired = (
                monotonic_ts() - ready_monotonic > self.partial_plan_max_age_seconds
            )
        if expired:
            return "plan_expired"
        partial = self._normalize_planning_text(plan.partial_text)
        final = self._normalize_planning_text(final_text)
        if len(partial) < 6:
            return "partial_too_short"
        if not final:
            return "final_text_empty"
        matched_chars = sum(
            block.size
            for block in SequenceMatcher(None, partial, final).get_matching_blocks()
        )
        final_coverage = matched_chars / max(1, len(final))
        if not (final.startswith(partial) or final_coverage >= 0.6):
            return "partial_final_mismatch"
        return ""

    @staticmethod
    def _normalize_planning_text(text: str) -> str:
        return re.sub(r"[\s，,。.!！?？：:；;]+", "", text).strip().lower()

    def _build_observer_context(
        self,
        *,
        user_text: str,
        interaction_seq: int,
        observer_version: int,
        input_kind: str,
    ) -> dict[str, Any]:
        field = self.ledger.current_field
        return {
            "interaction_seq": interaction_seq,
            "observer_version": observer_version,
            "state_version": self.ledger.state_version,
            "input_kind": input_kind,
            "interaction_mode": self.interaction_mode,
            "current_field_id": self.ledger.current_field_id,
            "current_field": field.snapshot() if field else None,
            "current_question": field.question_text if field else None,
            "user_text": user_text,
            "pending_clarification": self.ledger.pending_clarification,
            "recent_conversation": [
                {
                    "interaction_seq": item.interaction_seq,
                    "user_text": item.user_text,
                    "assistant_text": item.assistant_text,
                }
                for item in self.interaction_turns[-4:]
            ],
            "relevant_contextual_memories": self.blackboard.contextual_memories[-6:],
            "session_memory": self.session_memory.model_context(),
            "allowed_actions": [
                "ask_next_field",
                "ask_duration",
                "ask_frequency",
                "ask_most_of_day",
                "ask_impairment",
                "clarify_time_window",
                "repeat_current_question",
                "hold_for_assessor",
                "request_safety_review",
            ],
        }

    def _assessor_observer_context(
        self,
        field_id: str | None,
    ) -> dict[str, Any]:
        related_candidates = [
            item
            for item in self.blackboard.candidate_evidence
            if item.get("field_id") == field_id
        ][-8:]
        return {
            "status": "candidate_only",
            "warning": (
                "Observer data is uncommitted context. It may guide clarification "
                "but cannot independently justify a score."
            ),
            "candidate_evidence": related_candidates,
            "contextual_memories": self.blackboard.contextual_memories[-6:],
            "session_memory": self.session_memory.model_context(),
        }

    async def _generate_candidates(
        self,
        *,
        interaction_seq: int,
        field_id: str,
        state_version: int,
        directives: dict[str, DialogueDirective],
    ) -> dict[str, str]:
        trace = self._trace_for(interaction_seq)
        trace.candidate_generation_started_at = now_ts()
        try:
            envelope = self._causal_envelope(
                interaction_seq,
                field_id=field_id,
                state_version=state_version,
            )
            return await self._run_model_call(
                lambda: self.candidate_cache.pre_generate(
                    field_id=field_id,
                    state_version=state_version,
                    directives=directives,
                ),
                component="candidate_generator",
                purpose="candidate_generation",
                priority=ModelPriority.CANDIDATE_GENERATION,
                envelope=envelope,
            )
        finally:
            trace.candidate_generation_finished_at = now_ts()
            self._save_partial_snapshot()

    def claim_interaction_seq(self) -> int:
        """Reserve and return the next user-interaction sequence number."""

        self._latest_interaction_seq += 1
        seq = self._latest_interaction_seq
        self._sequence_states[seq] = "reserved"
        return seq

    async def _accept_interaction_seq(self, interaction_seq: int) -> bool:
        """Atomically accept a sequence exactly once."""

        _validate_interaction_seq(interaction_seq)
        if interaction_seq not in self._generation_by_seq:
            self._generation_by_seq[interaction_seq] = (
                self.session_actor.advance_ingress_generation()
            )
        async with self._sequence_lock:
            if self._closed or self._finished or self._terminal_pending_status:
                return False
            state = self._sequence_states.get(interaction_seq)
            if state in {"accepted", "done"}:
                self._trace_for(interaction_seq).error = "duplicate_interaction_seq"
                return False
            if interaction_seq < self._latest_interaction_seq:
                self._trace_for(interaction_seq).error = "out_of_order_interaction_seq"
                return False
            self._latest_interaction_seq = max(
                self._latest_interaction_seq,
                interaction_seq,
            )
            self._sequence_states[interaction_seq] = "accepted"
        await self._cancel_superseded_turn_supervisors(interaction_seq)
        await self._invalidate_superseded_assessments(interaction_seq)
        envelope = self._causal_envelope(interaction_seq)
        await self.session_actor.set_turn_phase(
            TurnPhase.ACCEPTED,
            interaction_seq=interaction_seq,
            envelope=envelope,
        )
        return not (self._closed or self._finished)

    async def _cancel_superseded_turn_supervisors(
        self,
        interaction_seq: int,
    ) -> None:
        """Cancel every older turn scope before accepting new ordinary work."""

        supervisors = [
            self._turn_supervisors.pop(seq)
            for seq in sorted(self._turn_supervisors)
            if seq < interaction_seq
        ]
        if supervisors:
            await asyncio.gather(
                *(supervisor.close() for supervisor in supervisors),
                return_exceptions=True,
            )

    async def _start_turn_supervisor(
        self,
        interaction_seq: int,
    ) -> TurnSupervisor:
        """Create the sole structured-concurrency owner for a runtime turn."""

        existing = self._turn_supervisors.get(interaction_seq)
        if existing is not None:
            return existing
        supervisor = TurnSupervisor(
            interaction_seq=interaction_seq,
            deadline_monotonic=(
                monotonic_ts() + self.runtime_policy.turn_deadline_seconds
            ),
        )
        await supervisor.start()
        self._turn_supervisors[interaction_seq] = supervisor
        return supervisor

    def _complete_interaction_seq(self, interaction_seq: int) -> None:
        if self._sequence_states.get(interaction_seq) == "accepted":
            self._sequence_states[interaction_seq] = "done"
            keep = self.runtime_policy.retention.recent_latency_traces
            if len(self._sequence_states) > keep:
                for seq in sorted(self._sequence_states)[:-keep]:
                    if self._sequence_states.get(seq) == "done":
                        self._sequence_states.pop(seq, None)
                        self._generation_by_seq.pop(seq, None)
            self._notify_actor(
                self.session_actor.set_turn_phase(
                    TurnPhase.COMPLETED,
                    interaction_seq=interaction_seq,
                    envelope=self._causal_envelope(interaction_seq),
                )
            )

    def is_latest_interaction(self, interaction_seq: int | None) -> bool:
        """Return whether a pending async task still belongs to the latest turn."""

        return (
            not self._closed
            and not self._finished
            and (
                interaction_seq is None
                or interaction_seq == self._latest_interaction_seq
            )
        )

    def _causal_envelope(
        self,
        interaction_seq: int,
        *,
        turn_id: int | None = None,
        field_id: str | None = None,
        state_version: int | None = None,
        causation_id: str | None = None,
    ) -> CausalEnvelope:
        return CausalEnvelope.for_turn(
            interaction_seq=interaction_seq,
            turn_id=turn_id,
            field_id=(self.ledger.current_field_id if field_id is None else field_id),
            state_version=(
                self.ledger.state_version if state_version is None else state_version
            ),
            generation=self._generation_by_seq.get(
                interaction_seq,
                self.session_actor.ingress_generation,
            ),
            timeout_seconds=self.runtime_policy.turn_deadline_seconds,
            causation_id=causation_id,
        )

    def _notify_actor(self, awaitable: Any) -> None:
        if self._closed or self.session_actor.closed:
            if hasattr(awaitable, "close"):
                awaitable.close()
            return
        try:
            task = asyncio.create_task(awaitable)
        except RuntimeError:
            if hasattr(awaitable, "close"):
                awaitable.close()
            return
        self._actor_notification_tasks.add(task)
        task.add_done_callback(self._actor_notification_tasks.discard)
        task.add_done_callback(self._consume_task_result)

    def _is_preemptive_control_text(self, text: str) -> bool:
        """Return whether mailbox pressure must not reject this input."""

        if self.safety_guard.classify(text).should_interrupt_sop:
            return True
        lowered = text.casefold()
        return any(token in lowered for token in _PREEMPTIVE_STOP_TOKENS)

    async def _busy_response(
        self,
        *,
        interaction_seq: int,
        reason: str,
    ) -> SCIDRuntimeResponse:
        """Reject one ordinary turn without consuming control-lane capacity."""

        async with self._sequence_lock:
            state = self._sequence_states.get(interaction_seq)
            if state in {"accepted", "done"} or (
                interaction_seq < self._latest_interaction_seq
            ):
                self._trace_for(interaction_seq).error = (
                    "busy_duplicate_or_out_of_order"
                )
                return SCIDRuntimeResponse(
                    interaction_seq=interaction_seq,
                    initial_stream=None,
                    action_stream_task=None,
                    stale=True,
                )
            self._latest_interaction_seq = interaction_seq
            self._sequence_states[interaction_seq] = "done"

        response_text = (
            _RUNTIME_PERSISTENCE_UNAVAILABLE_ZH
            if reason == "persistence_unavailable"
            else _RUNTIME_BUSY_ZH
        )
        turn = self._record_interaction(
            interaction_seq,
            "",
            ignored_reason=reason,
        )
        turn.assistant_text = response_text
        trace = self._trace_for(interaction_seq)
        trace.route = "runtime_busy"
        trace.error = reason
        record = self._record_runtime_turn(
            interaction_seq=interaction_seq,
            route="runtime_busy",
            initial_directive=None,
        )
        stream = self._stream_text(
            response_text,
            interaction_seq=interaction_seq,
            phase="initial",
            record=record,
            record_key="initial_text",
        )
        self._notify_actor(
            self.session_actor.emit(
                "InputRejectedBusy",
                payload={"reason": reason},
                envelope=self._causal_envelope(interaction_seq),
                lane=MailboxLane.WORK,
                coalesce_key="runtime-busy",
            )
        )
        self._save_partial_snapshot()
        return SCIDRuntimeResponse(
            interaction_seq=interaction_seq,
            initial_stream=stream,
            action_stream_task=None,
        )

    async def accept_text(
        self,
        user_text: str,
        *,
        interaction_seq: int,
    ) -> SCIDRuntimeResponse:
        """Accept one ASR-final interaction and return realtime streams."""

        if self._finished or self._closed or self._terminal_pending_status:
            return SCIDRuntimeResponse(
                interaction_seq=interaction_seq,
                initial_stream=None,
                action_stream_task=None,
                terminal=True,
            )
        if not self._started:
            await self.start()

        seq = interaction_seq
        text = _normalize_input_text(user_text)
        admission_block_reason = self.session_actor.normal_admission_block_reason
        if admission_block_reason and not self._is_preemptive_control_text(text):
            return await self._busy_response(
                interaction_seq=seq,
                reason=admission_block_reason,
            )
        if not await self._accept_interaction_seq(seq):
            return SCIDRuntimeResponse(
                interaction_seq=seq,
                initial_stream=None,
                action_stream_task=None,
                stale=True,
            )
        artifact_ref: str | None = None
        if self.persist_raw_transcript and text:
            artifact_ref = await self.event_store.append_artifact(
                interaction_seq=seq,
                role="user",
                text=text,
            )
        await self.session_actor.emit(
            "InputAccepted",
            payload={
                "character_count": len(text),
                "artifact_ref": artifact_ref,
            },
            envelope=self._causal_envelope(seq),
        )
        if len(text) > _MAX_FINAL_TEXT_CHARS:
            turn = self._record_interaction(
                seq,
                "",
                ignored_reason="input_too_long",
            )
            record = self._record_runtime_turn(
                interaction_seq=seq,
                route="input_too_long",
                initial_directive=None,
            )
            stream = self._stream_text(
                INPUT_TOO_LONG_ZH,
                interaction_seq=seq,
                phase="initial",
                record=record,
                record_key="initial_text",
            )
            turn.assistant_text = INPUT_TOO_LONG_ZH
            self._save_partial_snapshot()
            self._complete_interaction_seq(seq)
            return SCIDRuntimeResponse(
                interaction_seq=seq,
                initial_stream=stream,
                action_stream_task=None,
            )
        if self._is_obvious_asr_noise(text):
            await self.session_actor.call(
                "reject_noise_partial_plan",
                lambda: (
                    self.blackboard.reject_partial_plan("asr_noise"),
                    setattr(self.blackboard, "stable_partial_text", ""),
                ),
                event_type="PartialPlanRejected",
                event_payload={"reason": "asr_noise"},
                envelope=self._causal_envelope(seq),
            )
            self._trace_for(seq).partial_plan_rejected_reason = "asr_noise"
            turn = self._record_interaction(seq, text, ignored_reason="asr_noise")
            self._record_runtime_turn(
                interaction_seq=seq,
                route="asr_noise",
                initial_directive=None,
            )
            self._complete_interaction_seq(seq)
            self._save_partial_snapshot()
            return SCIDRuntimeResponse(
                interaction_seq=seq,
                initial_stream=None,
                action_stream_task=None,
                stale=turn.stale,
            )

        safety = self.safety_guard.classify(text)
        if safety.should_interrupt_sop:
            route = SCIDRouteDecision(
                route="crisis",
                confidence=0.95,
                should_score=False,
                normalized_user_text=text,
                safe_frontend_content="",
                reasoning_summary="Safety guard interrupted the SCID flow.",
                raw_payload={"safety_guard": True},
            )
            self._trace_for(seq).route = route.route
        else:
            route = await self._route_text_realtime(text, seq)

        envelope = self._causal_envelope(seq)
        await self.session_actor.set_turn_phase(
            TurnPhase.ROUTED,
            interaction_seq=seq,
            envelope=envelope,
        )
        await self.session_actor.emit(
            "RouteChosen",
            payload={
                "route": route.route,
                "should_score": route.should_score,
                "confidence": route.confidence,
            },
            envelope=envelope,
        )

        if self._closed or self._finished:
            return SCIDRuntimeResponse(
                interaction_seq=seq,
                initial_stream=None,
                action_stream_task=None,
                stale=True,
                terminal=True,
            )
        if not self.is_latest_interaction(seq):
            self.mark_interaction_stale(seq)
            self._complete_interaction_seq(seq)
            return SCIDRuntimeResponse(
                interaction_seq=seq,
                initial_stream=None,
                action_stream_task=None,
                stale=True,
            )

        if route.route in {"crisis", "stop_scid"}:
            self._cancel_active_assessments()
            self._cancel_active_observers()
            await self._cancel_speculation(reason=route.route)
        elif route.route == "scid_answer" and route.should_score:
            await self._promote_confirmed_speculative_reply()

        promoted_partial_plan: PartialObserverPlan | None = None
        if route.route == "scid_answer" and route.should_score:
            promoted_partial_plan = await self._promote_partial_plan_for_final(
                user_text=text,
                interaction_seq=seq,
            )
            rejection_reason = None
        else:
            rejection_reason = f"route_{route.route}"
            self._trace_for(seq).partial_plan_rejected_reason = rejection_reason

        turn = self._record_interaction(seq, text, route_decision=route)
        await self._actor_begin_blackboard_interaction(
            interaction_seq=seq,
            user_text=text,
            reject_partial_reason=rejection_reason,
        )
        turn_supervisor = (
            await self._start_turn_supervisor(seq)
            if route.route == "scid_answer" and route.should_score
            else None
        )
        observer_task = (
            None
            if route.route in {"crisis", "stop_scid"}
            else await self._start_observer_task(
                user_text=text,
                interaction_seq=seq,
                input_kind=("partial" if route.route == "scid_partial" else "final"),
                interaction_turn=turn,
                supervisor=turn_supervisor,
            )
        )
        if not self.is_latest_interaction(seq):
            turn.stale = True
            self.mark_interaction_stale(seq)
            self._save_partial_snapshot()
            return SCIDRuntimeResponse(
                interaction_seq=seq,
                initial_stream=None,
                action_stream_task=None,
                stale=True,
            )

        if route.route != "scid_answer" or not route.should_score:
            response = await self._handle_route(turn=turn, route=route)
            if self._closed or self._finished:
                return SCIDRuntimeResponse(
                    interaction_seq=seq,
                    initial_stream=None,
                    action_stream_task=None,
                    stale=True,
                    terminal=True,
                )
            record = self._record_runtime_turn(
                interaction_seq=seq,
                route=route.route,
                initial_directive=None,
            )
            stream = self._stream_text(
                response.final_text,
                interaction_seq=seq,
                phase="initial",
                record=record,
                record_key="initial_text",
            )
            self._save_partial_snapshot()
            return SCIDRuntimeResponse(
                interaction_seq=seq,
                initial_stream=stream if response.final_text else None,
                action_stream_task=None,
                stale=response.stale,
                terminal=bool(self._finished or self._terminal_pending_status),
            )

        self.interaction_mode = "scid"
        await self.session_actor.set_turn_phase(
            TurnPhase.WORKERS_RUNNING,
            interaction_seq=seq,
            envelope=envelope,
        )
        directive = self._build_realtime_initial_directive(turn=turn, route=route)
        record = self._record_runtime_turn(
            interaction_seq=seq,
            route=route.route,
            initial_directive=directive,
        )
        initial_boundary_event = asyncio.Event()
        initial_cancelled_event = asyncio.Event()
        initial_stream = self._stream_initial_semantic_segment(
            directive,
            interaction_seq=seq,
            context=self._foreground_context(turn=turn),
            record=record,
            boundary_event=initial_boundary_event,
            cancelled_event=initial_cancelled_event,
        )
        if turn_supervisor is None:
            raise RuntimeError("Scored SCID turn has no TurnSupervisor")
        action_stream_task = turn_supervisor.spawn(
            self._action_stream(
                turn=turn,
                route=route,
                record=record,
                observer_task=observer_task,
                promoted_partial_plan=promoted_partial_plan,
                initial_boundary_event=initial_boundary_event,
                initial_cancelled_event=initial_cancelled_event,
                supervisor=turn_supervisor,
            ),
            name=f"scid-action-stream-{seq}",
        )
        self._action_tasks.add(action_stream_task)
        action_stream_task.add_done_callback(self._action_tasks.discard)
        self._save_partial_snapshot()
        return SCIDRuntimeResponse(
            interaction_seq=seq,
            initial_stream=initial_stream,
            action_stream_task=action_stream_task,
        )

    async def _handle_route(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
    ) -> _RouteResponse:
        seq = turn.interaction_seq
        if self._closed or self._finished:
            return _RouteResponse(
                final_text="",
                stale=True,
                interaction_seq=seq,
            )
        logger.info(
            "SCID route handling - episode: %s, seq: %s, route: %s, should_score: %s",
            self.episode_id,
            seq,
            route.route,
            route.should_score,
        )

        if route.route == "crisis":
            final_text = await self._apply_crisis(turn, route)
            return self._final_response(turn, final_text)

        if route.route == "scid_partial":
            self.pending_user_buffer = self._combine_pending(
                self.pending_user_buffer,
                route.normalized_user_text or turn.user_text,
            )
            directive = DialogueDirective(
                directive_type="partial_ack",
                field_id=None,
                question_text=route.safe_frontend_content
                or "嗯，我在听，你可以继续说。",
                instruction="简短表示正在听，不要追问新问题，不要评分。",
                allowed_actions=["acknowledge"],
            )
            final_text = await self.dialogue_model.render(directive)
            return self._final_response(turn, final_text)

        if route.route == "question_clarification":
            directive = DialogueDirective(
                directive_type="question_clarification",
                field_id=self.ledger.current_field_id,
                question_text=route.safe_frontend_content
                or "我是在确认这个情况是否发生过。你可以按自己的理解说有、没有，或者举一个例子。",
                instruction="解释当前问题，不要评分，不要诊断。",
                allowed_actions=["clarify_wording", "resume_scid"],
            )
            final_text = await self.dialogue_model.render(directive)
            return self._final_response(turn, final_text)

        if route.route == "meta_question":
            directive = DialogueDirective(
                directive_type="meta_answer",
                field_id=None,
                question_text=route.safe_frontend_content
                or "这个阶段会按主题逐步确认，你可以随时暂停；准备好后我们再继续。",
                instruction="回答流程问题，不要透露分数、诊断或内部 JSON。",
                allowed_actions=["answer_meta", "resume_scid"],
            )
            final_text = await self.dialogue_model.render(directive)
            return self._final_response(turn, final_text)

        if route.route == "off_sop_chat":
            if self.interaction_mode != "paused":
                self.interaction_mode = "off_sop_chat"
            directive = DialogueDirective(
                directive_type="free_chat",
                field_id=None,
                question_text=turn.user_text,
                instruction=(
                    "用户暂时在聊评估外内容。只基于 question_text 做普通、简短回应；"
                    "不要提 SCID、量表、评估流程、分数或诊断。"
                ),
                allowed_actions=["free_chat", "respect_stop"],
            )
            final_text = await self.dialogue_model.render(directive)
            return self._final_response(turn, final_text)

        if route.route == "resume_scid":
            self.interaction_mode = "scid"
            self.pending_user_buffer = ""
            await self._actor_clear_foreground_probe(
                interaction_seq=turn.interaction_seq,
                reason="resume_scid",
            )
            directive = self._resume_directive(route)
            final_text = await self.dialogue_model.render(directive)
            return self._final_response(turn, final_text)

        if route.route == "pause_scid":
            self.interaction_mode = "paused"
            self.pending_user_buffer = ""
            await self._actor_clear_foreground_probe(
                interaction_seq=turn.interaction_seq,
                reason="pause_scid",
            )
            directive = DialogueDirective(
                directive_type="meta_answer",
                field_id=None,
                question_text=route.safe_frontend_content
                or "可以，我们先暂停一下。你想继续时直接说继续就好。",
                instruction="确认暂停，不要继续问 SCID 问题。",
                allowed_actions=["pause", "resume_scid"],
            )
            final_text = await self.dialogue_model.render(directive)
            return self._final_response(turn, final_text)

        if route.route == "stop_scid":
            self.interaction_mode = "completed"
            self.pending_user_buffer = ""
            await self._actor_clear_foreground_probe(
                interaction_seq=turn.interaction_seq,
                reason="stop_scid",
            )
            self.status = "stopped"
            final_text = SESSION_STOPPED_ZH
            turn.assistant_text = final_text
            self._request_terminal_finalize("stopped")
            return _RouteResponse(
                final_text=final_text,
                interaction_seq=turn.interaction_seq,
            )

        if route.route == "scid_answer" and route.should_score:
            self.interaction_mode = "scid"
            final_text = await self._score_scid_answer(turn=turn, route=route)
            return self._final_response(turn, final_text)

        directive = DialogueDirective(
            directive_type="meta_answer",
            field_id=None,
            question_text="我还需要再确认一下你的意思。你可以换个说法吗？",
            instruction="要求用户换一种说法，不要评分。",
            allowed_actions=["reask"],
        )
        final_text = await self.dialogue_model.render(directive)
        return self._final_response(turn, final_text)

    @staticmethod
    def _is_uncertain_reply(text: str) -> bool:
        return any(
            item in text
            for item in (
                "不确定",
                "不知道",
                "说不清",
                "不太确定",
                "应该",
                "好像",
                "可能",
                "大概",
                "也许",
                "似乎",
                "未必",
                "吧",
            )
        )

    def _fast_scan_negative_decision(
        self,
        *,
        request: AssessmentRequest,
    ) -> AssessmentDecision | None:
        """Return a local score-1 decision for obvious low-risk scan denials."""

        field = self.template.get_field(request.field_id)
        if field.kind != "scan" or field.safety_sensitive:
            return None
        text = request.user_text.strip()
        if not self._is_clear_negative_scan_answer(text):
            return None
        return AssessmentDecision(
            field_id=request.field_id,
            score="1",
            confidence=0.82,
            evidence=[request.user_text],
            next_action="advance",
            clarification_question="",
            reasoning_summary=(
                "用户对当前扫描题给出清楚的否定短答，使用本地低风险扫描否定规则推进。"
            ),
            raw_payload={"local_fast_scan_negative": True},
        )

    @staticmethod
    def _is_clear_negative_scan_answer(text: str) -> bool:
        stripped = text.strip().casefold()
        if not stripped or len(stripped) > 32:
            return False
        uncertain = (
            "不知道",
            "不确定",
            "不太确定",
            "说不清",
            "不清楚",
            "记不清",
            "没法说",
            "可能",
            "大概",
            "也许",
            "好像",
            "似乎",
            "未必",
            "吧",
        )
        if any(token in stripped for token in uncertain):
            return False
        contrast = ("但是", "但", "不过", "只是", "除非", "除了")
        if any(token in stripped for token in contrast):
            return False
        negative = (
            "完全没有",
            "完完全没有",
            "从来没有",
            "从未",
            "应该没有",
            "没有",
            "没",
            "否",
            "不是",
            "不算",
            "不会",
            "no",
            "never",
        )
        if not any(token in stripped for token in negative):
            return False
        positive = (
            "有过",
            "出现过",
            "发生过",
            "经常",
            "总是",
            "严重",
            "明显",
            "影响",
            "有时",
            "偶尔",
            "一点",
        )
        return not any(token in stripped for token in positive)

    def _bridge_fallback_text(
        self,
        text: str,
        *,
        interaction_seq: int | None = None,
    ) -> str:
        stripped = text.strip()
        variant = max(0, interaction_seq or 0)
        if self._is_uncertain_reply(stripped):
            options = (
                "我明白，你现在回想起来还保留一点不确定。我们先按你目前能确认的部分继续理解。",
                "听起来你目前更倾向于这个答案，但还不是完全确定。没关系，我们先沿着你现在的感受继续。",
                "可以的，有些经历确实很难一下回想得很确定。我们先以你此刻能想到的情况为准。",
            )
            return options[variant % len(options)]
        if any(item in stripped for item in ("没有", "完全没有", "完完全没有")):
            options = (
                "明白，就你现在回想起来，没有这样的体验。我先沿着你刚才表达的意思继续理解。",
                "好，我理解到的是，目前没有出现过这种情况。我们先保持在你刚才表达的这个意思上。",
                "听起来你目前的答案是否定的。我会按你刚才说的内容继续理解。",
            )
            return options[variant % len(options)]
        if any(item in stripped for item in ("有", "对", "是")):
            options = (
                "嗯，我听到了，你提到确实有过这样的体验。我会顺着你刚才说的内容继续理解。",
                "明白，这种情况对你来说是出现过的。我先把注意力放在你刚才确认的体验上。",
                "好，我理解了，你是在肯定这类经历。我们先沿着这个信息继续。",
            )
            return options[variant % len(options)]
        if len(stripped) >= 24:
            options = (
                "我听到了，也在顺着你刚才描述的情况理解。你提到的内容会放在完整语境里一起看。",
                "明白，你刚才讲的不只是一个简单答案。我先顺着这段经历和它的前后联系来理解。",
                "我在听，也注意到了你刚才描述里的具体情境。我们先保留这段完整的意思。",
            )
            return options[variant % len(options)]
        options = (
            "我听到了，也在顺着你刚才说的内容理解。我们先按你现在表达的意思继续。",
            "明白，我正在结合前后的语境理解你刚才这句话。",
            "好，我听懂了你现在想表达的意思。我们先从这里继续。",
        )
        return options[variant % len(options)]

    def _timeout_bridge_fallback_text(
        self,
        text: str,
        *,
        interaction_seq: int | None = None,
    ) -> str:
        """Return deterministic non-question wording when action selection times out."""

        del text
        variant = max(0, interaction_seq or 0)
        options = (
            "我还在把你刚才的回答和前后的语境对齐一下，我们先稳住这个节奏。",
            "我听到了，刚才这句我会放在完整语境里核对，我们先不急着下结论。",
            "这段信息我已经接住了，我会按你刚才表达的意思继续核对。",
        )
        return options[variant % len(options)]

    def _build_realtime_initial_directive(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
    ) -> DialogueDirective:
        text = route.normalized_user_text or turn.user_text
        current_question = ""
        if self.ledger.current_field_id:
            current_question = self.template.get_field(
                self.ledger.current_field_id
            ).question_text
        fallback_text = self._bridge_fallback_text(
            text,
            interaction_seq=turn.interaction_seq,
        )
        return DialogueDirective(
            directive_type="realtime_converse",
            field_id=None,
            question_text=(
                fallback_text
                if isinstance(self.dialogue_model, RuleBasedDialogueModel)
                else ""
            ),
            instruction=(
                "你是实时前台对话模型。只生成一个完整短句，先直接回应用户原话，"
                "再做有内容但克制的承接，整体适合一点五到两点五秒朗读。"
                "只能引用用户明确说过的内容，不要推测新的经历或感受。"
                "这一段不能提出新问题，也不能重问当前题。不要说已经记分、完成、"
                "进入下一题；不要评分、诊断或透露内部状态。"
            ),
            progress_text=(
                f"用户原话：{text}\n"
                f"当前访谈目标：围绕这个问题收集信息：{current_question}"
            ),
            allowed_actions=["converse", "respect_stop"],
        )

    def _foreground_context(self, *, turn: SCIDInteractionTurn) -> dict[str, Any]:
        current_field = self.ledger.current_field
        return {
            "interaction_seq": turn.interaction_seq,
            "user_text": turn.user_text,
            "current_question": current_field.question_text if current_field else "",
            "interaction_mode": self.interaction_mode,
            "recent_turns": [
                {
                    "user_text": item.user_text,
                    "assistant_text": item.assistant_text,
                }
                for item in self.interaction_turns[-6:]
            ],
            "allowed": [
                "自然回应",
                "基于用户原话进行克制复述",
                "回答流程体验",
                "说明之后可以补充或更正",
            ],
            "forbidden": [
                "提出新问题",
                "重问当前问题",
                "评分",
                "诊断",
                "字段编号",
                "内部 JSON",
                "承诺字段完成",
            ],
        }

    def _stream_foreground_action(
        self,
        action: ForegroundAction,
        *,
        record: dict[str, Any],
        rendered_text: str | None = None,
    ) -> AsyncIterator[str]:
        if action.directive.directive_type == "crisis":
            rendered_text = CRISIS_RESPONSE_ZH
        elif action.directive.directive_type == "complete":
            rendered_text = SESSION_COMPLETED_ZH
        if rendered_text is not None:
            return self._stream_text(
                rendered_text,
                interaction_seq=action.interaction_seq,
                phase="action",
                record=record,
                record_key="action_text",
            )
        return self._stream_dialogue_directive(
            action.directive,
            interaction_seq=action.interaction_seq,
            phase="action",
            context={
                "foreground_action": action.snapshot(),
                "safe_note": "Action is candidate-only unless source is assessor.",
            },
            record=record,
            record_key="action_text",
        )

    async def _stream_text(
        self,
        text: str,
        *,
        interaction_seq: int,
        phase: str,
        record: dict[str, Any] | None = None,
        record_key: str | None = None,
    ) -> AsyncIterator[str]:
        if self._closed or self._finished:
            return
        trace = self._trace_for(interaction_seq)
        if phase == "initial":
            trace.frontend_stream_started_at = now_ts()
            trace.frontend_initial_started_at = trace.frontend_stream_started_at
        else:
            trace.frontend_followup_started_at = now_ts()
        completed = False
        try:
            if text:
                if phase == "initial":
                    if trace.frontend_first_token_at is None:
                        trace.frontend_first_token_at = now_ts()
                else:
                    if trace.foreground_action_first_token_at is None:
                        trace.foreground_action_first_token_at = now_ts()
                    await self._ensure_speculation_spoken(interaction_seq)
                yield text
            completed = True
        finally:
            if not self._closed and not self._finished:
                if phase == "initial":
                    trace.frontend_initial_finished_at = now_ts()
                else:
                    trace.frontend_followup_finished_at = now_ts()
                if record is not None and record_key:
                    record[record_key] = text if completed else ""
                if record is not None:
                    record[f"{phase}_generation_complete"] = completed
                if completed and text and not self.session_actor.closed:
                    await self.session_actor.emit(
                        "ForegroundSegmentGenerated",
                        payload={
                            "segment_type": _audit_segment_type(phase, record),
                            "source": "runtime",
                            "delivery_status": "generated",
                            "character_count": len(text),
                            "action_id": _audit_selected_action_id(record),
                        },
                        envelope=self._causal_envelope(interaction_seq),
                    )
                self._save_partial_snapshot()

    async def _stream_initial_semantic_segment(
        self,
        directive: DialogueDirective,
        *,
        interaction_seq: int,
        context: dict[str, Any] | None,
        record: dict[str, Any],
        boundary_event: asyncio.Event,
        cancelled_event: asyncio.Event,
    ) -> AsyncIterator[str]:
        """Emit only the first complete initial sentence, then open arbitration."""

        source = self._stream_dialogue_directive(
            directive,
            interaction_seq=interaction_seq,
            phase="initial",
            context=context,
            record=None,
            record_key=None,
        )
        emitted: list[str] = []
        completed = False
        try:
            async for chunk in source:
                if self._closed or self._finished:
                    cancelled_event.set()
                    return
                text = str(chunk)
                boundary_index = next(
                    (
                        index
                        for index, char in enumerate(text)
                        if char in {"。", ".", "！", "!", "？", "?"}
                    ),
                    None,
                )
                if boundary_index is None:
                    emitted.append(text)
                    yield text
                    continue
                segment = text[: boundary_index + 1]
                if segment:
                    emitted.append(segment)
                    yield segment
                completed = True
                break
            else:
                completed = not self._trace_for(
                    interaction_seq
                ).frontend_stream_truncated
                if not completed:
                    cancelled_event.set()
        except asyncio.CancelledError:
            cancelled_event.set()
            raise
        finally:
            close = getattr(source, "aclose", None)
            if callable(close):
                with suppress(RuntimeError):
                    await close()
            if not self._closed and not self._finished:
                record["initial_text"] = "".join(emitted)
                if completed and self.is_latest_interaction(interaction_seq):
                    boundary_at = now_ts()
                    self._trace_for(interaction_seq).initial_semantic_boundary_at = (
                        boundary_at
                    )
                    record["initial_semantic_boundary_at"] = boundary_at
                else:
                    cancelled_event.set()
                if completed and emitted and not self.session_actor.closed:
                    await self.session_actor.emit(
                        "ForegroundSegmentGenerated",
                        payload={
                            "segment_type": "initial_ack",
                            "source": "talker",
                            "delivery_status": "generated",
                            "character_count": len("".join(emitted)),
                            "action_id": None,
                        },
                        envelope=self._causal_envelope(interaction_seq),
                    )
                self._save_partial_snapshot()
            else:
                cancelled_event.set()
            boundary_event.set()

    async def _stream_dialogue_directive(
        self,
        directive: DialogueDirective,
        *,
        interaction_seq: int,
        phase: str,
        context: dict[str, Any] | None,
        record: dict[str, Any] | None = None,
        record_key: str | None = None,
    ) -> AsyncIterator[str]:
        trace = self._trace_for(interaction_seq)
        if phase == "initial":
            trace.frontend_stream_started_at = now_ts()
            trace.frontend_initial_started_at = trace.frontend_stream_started_at
        else:
            trace.frontend_followup_started_at = now_ts()
        emitted: list[str] = []
        completed = False
        call_started = monotonic_ts()
        try:
            stream = self.dialogue_model.stream(directive, context=context)
            async for chunk in stream:
                if self._closed or self._finished:
                    return
                if not chunk:
                    continue
                if not emitted:
                    if phase == "initial":
                        if trace.frontend_first_token_at is None:
                            trace.frontend_first_token_at = now_ts()
                    else:
                        if trace.foreground_action_first_token_at is None:
                            trace.foreground_action_first_token_at = now_ts()
                        await self._ensure_speculation_spoken(interaction_seq)
                emitted.append(chunk)
                yield chunk
            if not emitted and not self._closed and not self._finished:
                fallback_text = await RuleBasedDialogueModel().render(directive)
                if fallback_text:
                    if phase == "initial":
                        if trace.frontend_first_token_at is None:
                            trace.frontend_first_token_at = now_ts()
                    else:
                        if trace.foreground_action_first_token_at is None:
                            trace.foreground_action_first_token_at = now_ts()
                        await self._ensure_speculation_spoken(interaction_seq)
                    emitted.append(fallback_text)
                    yield fallback_text
            completed = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._closed or self._finished:
                return
            error_type = type(exc).__name__
            error_message = (
                exc.error_message
                if isinstance(exc, FrontendStreamInterrupted)
                else "frontend model stream interrupted"
            )
            trace.frontend_stream_error = f"{error_type}: {error_message}"
            trace.frontend_stream_truncated = bool(emitted)
            logger.warning(
                "SCID frontend stream failed - episode: %s, seq: %s, "
                "phase: %s, after_output: %s, error: %s",
                self.episode_id,
                interaction_seq,
                phase,
                bool(emitted),
                error_type,
            )
            if not emitted:
                fallback_text = await RuleBasedDialogueModel().render(directive)
                if fallback_text:
                    if phase == "initial":
                        if trace.frontend_first_token_at is None:
                            trace.frontend_first_token_at = now_ts()
                    else:
                        if trace.foreground_action_first_token_at is None:
                            trace.foreground_action_first_token_at = now_ts()
                        await self._ensure_speculation_spoken(interaction_seq)
                    emitted.append(fallback_text)
                    yield fallback_text
                    completed = True
        finally:
            if not self._closed and not self._finished:
                if phase == "initial":
                    trace.frontend_initial_finished_at = now_ts()
                else:
                    trace.frontend_followup_finished_at = now_ts()
                if record is not None and record_key:
                    record[record_key] = "".join(emitted)
                if record is not None:
                    record[f"{phase}_generation_complete"] = completed
                if completed and emitted and not self.session_actor.closed:
                    await self.session_actor.emit(
                        "ModelCallCompleted",
                        payload={
                            "component": "talker",
                            "purpose": "foreground_generation",
                            "priority": "CRISIS_OR_FOREGROUND",
                            "status": "completed",
                            "total_latency_ms": round(
                                (monotonic_ts() - call_started) * 1000, 3
                            ),
                            "result_summary": {
                                "directive_type": directive.directive_type,
                                "streamed": True,
                            },
                        },
                        envelope=self._causal_envelope(interaction_seq),
                    )
                    await self.session_actor.emit(
                        "ForegroundSegmentGenerated",
                        payload={
                            "segment_type": _audit_segment_type(phase, record),
                            "source": "talker",
                            "delivery_status": "generated",
                            "character_count": len("".join(emitted)),
                            "action_id": _audit_selected_action_id(record),
                        },
                        envelope=self._causal_envelope(interaction_seq),
                    )
                self._save_partial_snapshot()

    async def _action_stream(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
        record: dict[str, Any],
        observer_task: (
            asyncio.Task[TurnInterpretation] | SupervisedTask[TurnInterpretation] | None
        ),
        promoted_partial_plan: PartialObserverPlan | None,
        initial_boundary_event: asyncio.Event,
        initial_cancelled_event: asyncio.Event,
        supervisor: TurnSupervisor,
    ) -> AsyncIterator[str] | None:
        seq = turn.interaction_seq
        source_field_id = self.ledger.current_field_id
        source_state_version = self.ledger.state_version
        trace = self._trace_for(seq)
        broker = ForegroundActionBroker(
            interaction_seq=seq,
            state_version=source_state_version,
            field_id=source_field_id,
        )
        assessment_task: SupervisedTask[Any] | None = None
        submitter: SupervisedTask[None] | None = None
        observer_submitter: SupervisedTask[None] | None = None
        resources_transferred_to_bridge = False

        try:
            if self._should_defer_for_speculation():
                await self.session_actor.call(
                    "defer_speculative_reply",
                    lambda: self.blackboard.defer_speculative_reply(
                        interaction_seq=seq,
                        user_text=self._scoring_text_for(route, turn.user_text),
                    ),
                    event_type="SpeculativeReplyDeferred",
                    event_payload={},
                    envelope=self._causal_envelope(seq),
                )
                text = await self._resolve_speculative_reply(
                    turn=turn,
                    route=route,
                    record=record,
                )
                if not text:
                    return None
                deferred_action = ForegroundAction(
                    interaction_seq=seq,
                    based_on_state_version=self.ledger.state_version,
                    field_id=self.ledger.current_field_id,
                    kind=(
                        "repair" if record.get("repair_required") else "ask_committed"
                    ),
                    source="assessor",
                    priority=90,
                    directive=DialogueDirective(
                        directive_type="followup_after_commit",
                        field_id=self.ledger.current_field_id,
                        question_text=text,
                        instruction="直接朗读后台已确认的下一步文本，不要添加诊断信息。",
                    ),
                )
                await initial_boundary_event.wait()
                if initial_cancelled_event.is_set() or not self.is_latest_interaction(
                    seq
                ):
                    return None
                selected_at = now_ts()
                trace.foreground_action_selected_at = selected_at
                trace.action_delivery_status = "selected"
                self._record_foreground_action(deferred_action)
                await self._actor_select_action(
                    deferred_action,
                    envelope=self._causal_envelope(seq),
                )
                record["selected_action"] = deferred_action.snapshot()
                record["action_delivery_status"] = "selected"
                record["selection_reason"] = "deferred_speculation_resolved"
                return self._stream_foreground_action(
                    deferred_action,
                    record=record,
                    rendered_text=text,
                )

            assessment_started = asyncio.Event()
            self._assessment_model_started[seq] = assessment_started
            assessment_task = supervisor.spawn(
                self._score_scid_answer(
                    turn=turn,
                    route=route,
                    allow_speculative_commit=True,
                    render_response=False,
                ),
                name=f"scid-assessment-{seq}",
            )
            self._assessment_tasks.add(assessment_task)
            assessment_task.add_done_callback(self._assessment_tasks.discard)
            await assessment_task.wait_started()
            await assessment_started.wait()

            submitter = supervisor.spawn(
                self._submit_assessment_foreground_action(
                    broker=broker,
                    assessment_task=assessment_task,
                    turn=turn,
                    record=record,
                ),
                name=f"scid-assessment-submitter-{seq}",
            )
            self._foreground_worker_tasks.add(submitter)
            submitter.add_done_callback(self._foreground_worker_tasks.discard)
            if observer_task is not None and self.observer_mode == "active":
                observer_submitter = supervisor.spawn(
                    self._submit_observer_foreground_action(
                        broker=broker,
                        observer_task=observer_task,
                        interaction_seq=seq,
                        source_field_id=source_field_id,
                        source_state_version=source_state_version,
                    ),
                    name=f"scid-observer-submitter-{seq}",
                )
                self._foreground_worker_tasks.add(observer_submitter)
                observer_submitter.add_done_callback(
                    self._foreground_worker_tasks.discard
                )

            if promoted_partial_plan is not None:
                partial_interpretation = promoted_partial_plan.interpretation
                partial_action = self._build_observer_foreground_action(
                    interpretation=partial_interpretation,
                    interaction_seq=seq,
                    source_field_id=source_field_id,
                    source_state_version=source_state_version,
                    source="observer_partial",
                    provisional=True,
                    candidate_text=promoted_partial_plan.candidate_texts.get(
                        partial_interpretation.recommended_action
                    ),
                )
                if partial_action is not None and await broker.submit(partial_action):
                    trace.foreground_action_ready_at = now_ts()
                    trace.foreground_action_source = partial_action.source
                    trace.foreground_action_kind = partial_action.kind

            # Spawning is mailbox-based so the supervisor owner remains the
            # sole task-group mutator. Give newly admitted submitters a chance
            # to supersede provisional candidates when the boundary fired.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            trace.fast_policy_started_at = now_ts()
            fast_action = self._build_fast_policy_action(
                interaction_seq=seq,
                state_version=source_state_version,
                user_text=self._scoring_text_for(route, turn.user_text),
            )
            trace.fast_policy_finished_at = now_ts()
            if fast_action is not None:
                trace.foreground_action_ready_at = now_ts()
                trace.foreground_action_source = fast_action.source
                trace.foreground_action_kind = fast_action.kind
                await broker.submit(fast_action)

            trace.broker_wait_started_at = now_ts()
            await initial_boundary_event.wait()
            if initial_cancelled_event.is_set() or not self.is_latest_interaction(seq):
                trace.realtime_cancelled = initial_cancelled_event.is_set()
                trace.stale = not self.is_latest_interaction(seq)
                record["action_stale"] = trace.stale
                await self._cancel_action_workers(
                    assessment_task,
                    submitter,
                    observer_submitter,
                )
                return None
            # Let backend tasks that became ready during the final initial chunk
            # publish their candidates before the semantic boundary is locked.
            await asyncio.sleep(0)
            trace.post_boundary_wait_started_at = now_ts()
            action = await broker.commit_best(
                timeout=self.post_initial_action_wait_seconds
            )
            trace.post_boundary_wait_finished_at = now_ts()
            trace.broker_wait_finished_at = trace.post_boundary_wait_finished_at
            if action is None:
                trace.stale = False
                resources_transferred_to_bridge = True
                return self._stream_timeout_bridge_then_action(
                    turn=turn,
                    route=route,
                    record=record,
                    broker=broker,
                    assessment_task=assessment_task,
                    submitter=submitter,
                    observer_submitter=observer_submitter,
                    initial_cancelled_event=initial_cancelled_event,
                    supervisor=supervisor,
                    source_field_id=source_field_id,
                    source_state_version=source_state_version,
                )

            return await self._select_and_stream_real_action(
                action=action,
                broker=broker,
                assessment_task=assessment_task,
                submitter=submitter,
                observer_submitter=observer_submitter,
                turn=turn,
                route=route,
                record=record,
                supervisor=supervisor,
                source_field_id=source_field_id,
                source_state_version=source_state_version,
                selection_reason="highest_priority_at_initial_boundary",
            )
        except asyncio.CancelledError:
            if not self._closed and not self._finished:
                trace.realtime_cancelled = True
            with suppress(asyncio.CancelledError):
                await self._cancel_action_workers(
                    assessment_task,
                    submitter,
                    observer_submitter,
                )
            if not self._closed and not self._finished:
                self._save_partial_snapshot()
            return None
        finally:
            self._assessment_model_started.pop(seq, None)
            if not resources_transferred_to_bridge:
                await broker.close()
                await self._cancel_action_workers(submitter, observer_submitter)

    async def _stream_timeout_bridge_then_action(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
        record: dict[str, Any],
        broker: ForegroundActionBroker,
        assessment_task: SupervisedTask[Any],
        submitter: SupervisedTask[None],
        observer_submitter: SupervisedTask[None] | None,
        initial_cancelled_event: asyncio.Event,
        supervisor: TurnSupervisor,
        source_field_id: str | None,
        source_state_version: int,
    ) -> AsyncIterator[str]:
        """Emit a non-terminal bridge, then keep waiting for a real action."""

        seq = turn.interaction_seq
        trace = self._trace_for(seq)
        try:
            if initial_cancelled_event.is_set() or not self.is_latest_interaction(seq):
                trace.realtime_cancelled = initial_cancelled_event.is_set()
                trace.stale = not self.is_latest_interaction(seq)
                record["action_stale"] = trace.stale
                return

            bridge_directive = await self._record_timeout_bridge(
                interaction_seq=seq,
                source_text=self._scoring_text_for(route, turn.user_text),
                record=record,
            )
            async for chunk in self._stream_dialogue_directive(
                bridge_directive,
                interaction_seq=seq,
                phase="bridge",
                context={
                    "timeout_bridge": {
                        "source": "timeout_fallback",
                        "non_terminal": True,
                    }
                },
                record=record,
                record_key="bridge_text",
            ):
                if initial_cancelled_event.is_set() or not self.is_latest_interaction(
                    seq
                ):
                    trace.realtime_cancelled = initial_cancelled_event.is_set()
                    trace.stale = not self.is_latest_interaction(seq)
                    record["action_stale"] = trace.stale
                    return
                yield chunk

            record["bridge_emitted_at"] = now_ts()
            self._save_partial_snapshot()
            if initial_cancelled_event.is_set() or not self.is_latest_interaction(seq):
                trace.realtime_cancelled = initial_cancelled_event.is_set()
                trace.stale = not self.is_latest_interaction(seq)
                record["action_stale"] = trace.stale
                return

            action: ForegroundAction | None = None
            while True:
                if initial_cancelled_event.is_set() or not self.is_latest_interaction(
                    seq
                ):
                    trace.realtime_cancelled = initial_cancelled_event.is_set()
                    trace.stale = not self.is_latest_interaction(seq)
                    record["action_stale"] = trace.stale
                    return
                remaining = max(0.0, supervisor.deadline_monotonic - monotonic_ts())
                if remaining <= 0:
                    trace.broker_wait_finished_at = now_ts()
                    record["selection_reason"] = "timeout_bridge_deadline_expired"
                    return
                action = await broker.commit_best(timeout=min(remaining, 0.05))
                if action is not None:
                    break

            trace.broker_wait_finished_at = now_ts()
            if action is None:
                record["selection_reason"] = "timeout_bridge_no_backend_action"
                return

            stream = await self._select_and_stream_real_action(
                action=action,
                broker=broker,
                assessment_task=assessment_task,
                submitter=submitter,
                observer_submitter=observer_submitter,
                turn=turn,
                route=route,
                record=record,
                supervisor=supervisor,
                source_field_id=source_field_id,
                source_state_version=source_state_version,
                selection_reason="after_timeout_bridge",
            )
            if stream is None:
                return

            real_action_started = False
            async for chunk in stream:
                if initial_cancelled_event.is_set() or not self.is_latest_interaction(
                    seq
                ):
                    trace.realtime_cancelled = initial_cancelled_event.is_set()
                    trace.stale = not self.is_latest_interaction(seq)
                    record["action_stale"] = trace.stale
                    return
                if chunk and not real_action_started:
                    real_action_started = True
                    self.mark_followup_segment_published(seq)
                yield chunk
        except asyncio.CancelledError:
            if not self._closed and not self._finished:
                trace.realtime_cancelled = True
                self._save_partial_snapshot()
            raise
        finally:
            await broker.close()
            if self._pending_speculative_assessment is assessment_task:
                await self._cancel_action_workers(submitter, observer_submitter)
            else:
                await self._cancel_action_workers(
                    assessment_task,
                    submitter,
                    observer_submitter,
                )

    async def _select_and_stream_real_action(
        self,
        *,
        action: ForegroundAction,
        broker: ForegroundActionBroker,
        assessment_task: SupervisedTask[Any],
        submitter: SupervisedTask[None],
        observer_submitter: SupervisedTask[None] | None,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
        record: dict[str, Any],
        supervisor: TurnSupervisor,
        source_field_id: str | None,
        source_state_version: int,
        selection_reason: str,
    ) -> AsyncIterator[str] | None:
        """Apply real-action arbitration side effects and return its stream."""

        seq = turn.interaction_seq
        trace = self._trace_for(seq)
        rendered_action_text: str | None = None
        candidate_sources = {"fast_policy", "observer", "observer_partial"}
        if action.source in candidate_sources and assessment_task.done():
            if assessment_task.cancelled():
                return None
            assessment_result = await assessment_task
            if not assessment_result:
                return None
            if isinstance(assessment_result, DialogueDirective):
                assessment_directive = assessment_result
            else:
                rendered_action_text = str(assessment_result)
                assessment_directive = DialogueDirective(
                    directive_type="followup_after_commit",
                    field_id=self.ledger.current_field_id,
                    question_text=rendered_action_text,
                    instruction=("直接朗读后台已确认的下一步文本，不要添加诊断信息。"),
                )
            action = ForegroundAction(
                interaction_seq=seq,
                based_on_state_version=source_state_version,
                field_id=source_field_id,
                kind=self._committed_action_kind(
                    assessment_directive,
                    repair_required=(
                        record.get("repair_required", False) or trace.repair_required
                    ),
                ),
                source="assessor",
                priority=90,
                directive=assessment_directive,
            )
        elif (
            action.source in {"observer", "observer_partial"} and not action.speculative
        ):
            assessment_result, committed = await self._cancel_or_collect_assessment(
                assessment_task,
                turn=turn,
            )
            submitter.cancel()
            await self._cancel_and_drain({submitter})
            if committed and assessment_result:
                if isinstance(assessment_result, DialogueDirective):
                    assessment_directive = assessment_result
                else:
                    rendered_action_text = str(assessment_result)
                    assessment_directive = DialogueDirective(
                        directive_type="followup_after_commit",
                        field_id=self.ledger.current_field_id,
                        question_text=rendered_action_text,
                        instruction=(
                            "直接朗读后台已确认的下一步文本，不要添加诊断信息。"
                        ),
                    )
                action = ForegroundAction(
                    interaction_seq=seq,
                    based_on_state_version=source_state_version,
                    field_id=source_field_id,
                    kind=self._committed_action_kind(
                        assessment_directive,
                        repair_required=trace.repair_required,
                    ),
                    source="assessor",
                    priority=90,
                    directive=assessment_directive,
                )
            else:
                if (
                    not self.is_latest_interaction(seq)
                    or self.ledger.current_field_id != source_field_id
                    or self.ledger.state_version != source_state_version
                ):
                    return None
                source_text = self._scoring_text_for(route, turn.user_text)
                self.pending_user_buffer = self._combine_pending(
                    self.pending_user_buffer,
                    source_text,
                )
                probe_payload = {
                    "interaction_seq": seq,
                    "field_id": source_field_id,
                    "state_version": source_state_version,
                    "observer_action": action.directive.progress_text,
                    "question_text": action.directive.question_text,
                    "source_user_text": source_text,
                    "source": action.source,
                    "evidence_slot": action.evidence_slot,
                }
                await self.session_actor.call(
                    "defer_for_observer_probe",
                    lambda: (
                        self.blackboard.request_foreground_probe(probe_payload),
                        setattr(
                            self.blackboard,
                            "assessor_status",
                            "deferred_for_observer_probe",
                        ),
                    ),
                    event_type="ForegroundProbeRequested",
                    event_payload={"source": action.source},
                    envelope=self._causal_envelope(seq),
                )
                trace.stale = False

        if action.source in candidate_sources and (
            not self.is_latest_interaction(seq)
            or self.ledger.current_field_id != source_field_id
            or self.ledger.state_version != source_state_version
        ):
            return None

        if observer_submitter is not None and not observer_submitter.done():
            observer_submitter.cancel()
            with suppress(asyncio.CancelledError):
                await observer_submitter

        broker_snapshot = broker.snapshot()
        selected_at = now_ts()
        trace.foreground_action_selected_at = selected_at
        trace.action_delivery_status = "selected"
        trace.foreground_action_source = action.source
        trace.foreground_action_kind = action.kind
        trace.foreground_action_superseded_count = len(broker_snapshot["superseded"])
        self._record_foreground_action(action)
        await self._actor_select_action(
            action,
            envelope=self._causal_envelope(
                seq,
                field_id=source_field_id,
                state_version=source_state_version,
            ),
        )
        record["selected_action"] = action.snapshot()
        record["action_delivery_status"] = "selected"
        record["selection_reason"] = selection_reason
        record["broker"] = broker_snapshot
        if (
            action.speculative
            and source_field_id is not None
            and action.directive.field_id is not None
            and self.ledger.current_field_id == source_field_id
            and self.ledger.state_version == source_state_version
            and self.blackboard.speculative_depth == 0
        ):
            await self._begin_speculation(
                action=action,
                source_field_id=source_field_id,
                speculative_field_id=action.directive.field_id,
                source_state_version=source_state_version,
            )
            trace.speculative_advance = True
            trace.speculative_field_id = action.directive.field_id
            self._pending_speculative_assessment = assessment_task
            self._turn_supervisors.pop(seq, None)
            self._speculation_supervisor = supervisor
            watcher = asyncio.create_task(
                self._watch_speculative_assessment(
                    assessment_task=assessment_task,
                    source_field_id=source_field_id,
                    speculative_field_id=action.directive.field_id,
                    interaction_seq=seq,
                )
            )
            self._speculative_watcher_tasks.add(watcher)
            watcher.add_done_callback(self._speculative_watcher_tasks.discard)

        return self._stream_foreground_action(
            action,
            record=record,
            rendered_text=(
                rendered_action_text
                if rendered_action_text is not None
                else (
                    action.directive.question_text
                    if action.source in {"observer", "observer_partial"}
                    else None
                )
            ),
        )

    async def _cancel_action_workers(
        self,
        *tasks: asyncio.Task[Any] | None,
    ) -> None:
        active = [task for task in tasks if task is not None and not task.done()]
        for task in active:
            self._invalidate_assessment_task(task)
            task.cancel()
        if active:
            await self._cancel_and_drain(set(active))

    async def _cancel_or_collect_assessment(
        self,
        task: asyncio.Task[Any],
        *,
        turn: SCIDInteractionTurn,
    ) -> tuple[Any | None, bool]:
        """Cancel assessment work, then distinguish a raced commit from discard."""

        owner_token = self._assessment_owner_by_task.get(task)
        owner = (
            self._assessment_owners.get(owner_token)
            if owner_token is not None
            else None
        )
        if owner is None:
            owner = next(
                (
                    candidate
                    for candidate in self._assessment_owners.values()
                    if candidate.interaction_seq == turn.interaction_seq
                    and (
                        turn.scid_turn_id is None
                        or candidate.turn_id == turn.scid_turn_id
                    )
                ),
                None,
            )
        if owner is not None:
            owner.active = False
        if not task.done():
            self._invalidate_assessment_task(task)
            task.cancel()
            await self._cancel_and_drain({task})

        result: Any | None = None
        if task.done() and not task.cancelled():
            with suppress(Exception):
                result = task.result()

        committed = False
        async with self._ledger_lock:
            if owner is None:
                owner = next(
                    (
                        candidate
                        for candidate in self._assessment_owners.values()
                        if candidate.interaction_seq == turn.interaction_seq
                    ),
                    None,
                )
                if owner is not None:
                    owner.active = False
            ledger_turn = next(
                (
                    candidate
                    for candidate in self.ledger.turns
                    if candidate.turn_id == turn.scid_turn_id
                ),
                None,
            )
            committed = ledger_turn is not None and ledger_turn.decision is not None
            if committed and result is None:
                result = self.ledger.get_directive()
            elif not committed:
                discarded = await self._actor_discard_interaction_turn(
                    turn.interaction_seq
                )
                if discarded:
                    turn.scid_turn_id = None
                    if owner is not None:
                        self._release_assessment_owner(owner, task)
            await self._discard_inactive_assessment_turns()
        return result, committed

    def _observer_candidate_directive(
        self,
        *,
        interpretation: TurnInterpretation,
        field: Any,
    ) -> DialogueDirective | None:
        """Build a safe action directive without granting scoring authority."""

        action = interpretation.recommended_action
        slot = _OBSERVER_SLOT_BY_ACTION.get(action)
        if slot is not None:
            if (
                slot not in interpretation.missing_slots
                or interpretation.commit_required
                or field.safety_sensitive
                or interpretation.current_field_relevance < 0.75
                or interpretation.confidence
                < self.latency_controller.observer_confidence_threshold
            ):
                return None
            return DialogueDirective(
                directive_type="observer_probe",
                field_id=field.field_id,
                question_text=_OBSERVER_PROBE_QUESTIONS[action],
                instruction=(
                    "只问这个同字段安全追问，不要增加第二个问题，不要宣布字段完成。"
                ),
                progress_text=action,
                allowed_actions=["clarify"],
            )

        if action != "ask_next_field":
            return None
        plan = self.latency_controller.plan(
            field=field,
            interpretation=interpretation,
            allow_one_step_speculation=self.allow_one_step_speculation,
            speculative_depth=self.blackboard.speculative_depth,
            repair_pending=self.blackboard.repair_pending is not None,
        )
        next_field_id = self.ledger.preview_next_scan_field_id(field.field_id)
        if not plan.allow_speculation or next_field_id is None:
            return None
        next_field = self.template.get_field(next_field_id)
        return DialogueDirective(
            directive_type="candidate_question",
            field_id=next_field_id,
            question_text=next_field.question_text,
            instruction=("自然问出 Observer 建议的候选下一题。不要表示上一题已完成。"),
            allowed_actions=["ask"],
        )

    async def _submit_observer_foreground_action(
        self,
        *,
        broker: ForegroundActionBroker,
        observer_task: asyncio.Task[TurnInterpretation],
        interaction_seq: int,
        source_field_id: str | None,
        source_state_version: int,
    ) -> None:
        """Convert one current Observer recommendation into a broker action."""

        try:
            interpretation = await observer_task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "SCID realtime observer action failed - episode: %s, seq: %s",
                self.episode_id,
                interaction_seq,
            )
            return
        action = self._build_observer_foreground_action(
            interpretation=interpretation,
            interaction_seq=interaction_seq,
            source_field_id=source_field_id,
            source_state_version=source_state_version,
        )
        if action is None:
            return
        if source_field_id is not None:
            candidate_text = self.candidate_cache.get(
                field_id=source_field_id,
                state_version=source_state_version,
                action=interpretation.recommended_action,
            )
            if candidate_text:
                action.directive = replace(
                    action.directive,
                    question_text=candidate_text,
                )
        accepted = await broker.submit(action)
        if not accepted:
            return
        trace = self._trace_for(interaction_seq)
        trace.foreground_action_ready_at = now_ts()
        trace.foreground_action_source = action.source
        trace.foreground_action_kind = action.kind

    def _build_observer_foreground_action(
        self,
        *,
        interpretation: TurnInterpretation,
        interaction_seq: int,
        source_field_id: str | None,
        source_state_version: int,
        source: str = "observer",
        provisional: bool = False,
        candidate_text: str | None = None,
    ) -> ForegroundAction | None:
        """Build one safe Observer action without granting scoring authority."""

        field = self.ledger.current_field
        if (
            interpretation.stale
            or interpretation.interaction_seq != interaction_seq
            or interpretation.based_on_state_version != source_state_version
            or interpretation.field_id != source_field_id
            or not self.is_latest_interaction(interaction_seq)
            or field is None
            or field.field_id != source_field_id
            or self.ledger.state_version != source_state_version
            or self.blackboard.repair_pending is not None
            or self.blackboard.pending_foreground_probe is not None
            or self.blackboard.speculative_depth > 0
        ):
            return None

        directive = self._observer_candidate_directive(
            interpretation=interpretation,
            field=field,
        )
        if directive is None:
            return None
        if candidate_text:
            directive = replace(directive, question_text=candidate_text)
        speculative = interpretation.recommended_action == "ask_next_field"
        slot = _OBSERVER_SLOT_BY_ACTION.get(interpretation.recommended_action)
        return ForegroundAction(
            interaction_seq=interaction_seq,
            based_on_state_version=source_state_version,
            field_id=source_field_id,
            kind="ask_candidate" if speculative else "clarify",
            source=source,
            priority=(55 if speculative else 60 if provisional else 65),
            directive=directive,
            evidence_slot=slot,
            provisional=provisional,
            source_observer_version=interpretation.observer_version,
            speculative=speculative,
        )

    async def _submit_assessment_foreground_action(
        self,
        *,
        broker: ForegroundActionBroker,
        assessment_task: asyncio.Task[Any],
        turn: SCIDInteractionTurn,
        record: dict[str, Any],
    ) -> None:
        try:
            result = await asyncio.shield(assessment_task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._closed or self._finished:
                return
            trace = self._trace_for(turn.interaction_seq)
            trace.error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "SCID realtime assessment action failed - episode: %s, seq: %s",
                self.episode_id,
                turn.interaction_seq,
            )
            result = "刚才后台流程出现了技术问题，我们先暂停一下，我会重新核对状态。"
            record["repair_required"] = True
        if not result or not self.is_latest_interaction(turn.interaction_seq):
            return
        if isinstance(result, DialogueDirective):
            directive = result
        else:
            directive = DialogueDirective(
                directive_type="followup_after_commit",
                field_id=self.ledger.current_field_id,
                question_text=str(result),
                instruction="直接朗读后台已确认的下一步文本，不要添加诊断信息。",
            )
        trace = self._trace_for(turn.interaction_seq)
        kind = self._committed_action_kind(
            directive,
            repair_required=(
                record.get("repair_required", False) or trace.repair_required
            ),
        )
        action = ForegroundAction(
            interaction_seq=turn.interaction_seq,
            based_on_state_version=broker.state_version,
            field_id=broker.field_id,
            kind=kind,
            source="assessor",
            priority=90,
            directive=directive,
            terminal=kind in {"crisis", "complete"},
        )
        trace.foreground_action_ready_at = now_ts()
        trace.foreground_action_source = action.source
        trace.foreground_action_kind = action.kind
        await broker.submit(action)

    @staticmethod
    def _committed_action_kind(
        directive: DialogueDirective,
        *,
        repair_required: bool,
    ) -> str:
        if directive.directive_type == "crisis":
            return "crisis"
        if directive.directive_type == "complete":
            return "complete"
        if repair_required or directive.directive_type == "repair_prompt":
            return "repair"
        if directive.directive_type == "clarify":
            return "clarify"
        return "ask_committed"

    def _build_fast_policy_action(
        self,
        *,
        interaction_seq: int,
        state_version: int,
        user_text: str,
    ) -> ForegroundAction | None:
        if not self.allow_one_step_speculation:
            return None
        field = self.ledger.current_field
        next_field = None
        if field is not None:
            next_field_id = self.ledger.preview_next_scan_field_id(field.field_id)
            next_field = (
                self.template.get_field(next_field_id) if next_field_id else None
            )
        return self.fast_policy.action_for_scan_answer(
            interaction_seq=interaction_seq,
            state_version=state_version,
            field=field,
            next_field=next_field,
            speculative_depth=self.blackboard.speculative_depth,
            repair_pending=self.blackboard.repair_pending is not None,
            user_text=user_text,
        )

    async def _record_timeout_bridge(
        self,
        *,
        interaction_seq: int,
        source_text: str,
        record: dict[str, Any],
    ) -> DialogueDirective:
        """Record and build a non-terminal bridge for slow backend actions."""

        bridge_text = self._timeout_bridge_fallback_text(
            source_text,
            interaction_seq=interaction_seq,
        )
        record["bridge_source"] = "timeout_fallback"
        record["bridge_requested_at"] = now_ts()
        await self.session_actor.call(
            "request_timeout_bridge",
            lambda: setattr(
                self.blackboard,
                "assessor_status",
                "deferred_for_timeout_bridge",
            ),
            event_type="ForegroundBridgeRequested",
            event_payload={"source": "timeout_fallback"},
            envelope=self._causal_envelope(interaction_seq),
        )
        return DialogueDirective(
            directive_type="realtime_converse",
            field_id=None,
            question_text=(
                bridge_text
                if isinstance(self.dialogue_model, RuleBasedDialogueModel)
                else ""
            ),
            instruction=(
                "后台还没有给出可提交动作。请只生成一句自然、简短的承接，"
                "降低等待感；不要提出新问题，不要要求用户举例，不要重问当前题，"
                "不要说字段完成、评分或诊断。"
            ),
            progress_text=(
                "用户刚才已经回答当前问题；只做自然等待承接，不问补充问题。"
            ),
            allowed_actions=["converse", "respect_stop"],
        )

    @overload
    async def _score_scid_answer(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
        allow_speculative_commit: bool = False,
        render_response: Literal[True] = True,
    ) -> str: ...

    @overload
    async def _score_scid_answer(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
        allow_speculative_commit: bool = False,
        render_response: Literal[False],
    ) -> str | DialogueDirective: ...

    async def _score_scid_answer(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
        allow_speculative_commit: bool = False,
        render_response: bool = True,
    ) -> str | DialogueDirective:
        seq = turn.interaction_seq
        trace = self._trace_for(seq)
        scorer_text = self._scoring_text_for(route, turn.user_text)
        source_field_id: str | None
        source_state_version: int
        owner: _AssessmentOwner

        assessment_task = asyncio.current_task()
        async with self._ledger_lock:
            await self._discard_inactive_assessment_turns()
            source_field_id = self.ledger.current_field_id
            source_state_version = self.ledger.state_version
            if not self._assessment_result_is_applicable(
                interaction_seq=seq,
                source_field_id=source_field_id,
                source_state_version=source_state_version,
                allow_speculative_commit=allow_speculative_commit,
            ):
                turn.stale = True
                trace.stale = True
                return ""
            request_envelope = self._causal_envelope(
                seq,
                field_id=source_field_id,
                state_version=source_state_version,
            )
            ledger_turn = await self._actor_begin_turn(
                user_text=scorer_text,
                interaction_seq=seq,
                envelope=request_envelope,
            )
            turn.scid_turn_id = ledger_turn.turn_id
            owner = _AssessmentOwner(
                token=str(uuid4()),
                interaction_seq=seq,
                turn_id=ledger_turn.turn_id,
                field_id=source_field_id or "",
                state_version=source_state_version,
            )
            self._assessment_owners[owner.token] = owner
            if assessment_task is not None:
                self._assessment_owner_by_task[assessment_task] = owner.token
            request = AssessmentRequest(
                interaction_seq=seq,
                turn_id=ledger_turn.turn_id,
                field_id=source_field_id or "",
                state_version=source_state_version,
                owner_token=owner.token,
                user_text=scorer_text,
                assessor_context=copy.deepcopy(
                    self.ledger.build_assessor_context(ledger_turn)
                ),
                observer_context=copy.deepcopy(
                    self._assessor_observer_context(source_field_id)
                ),
            )
            await self._actor_set_assessor_status(
                "running",
                field_id=source_field_id,
                envelope=request_envelope,
            )
            await self.session_actor.set_assessment_phase(
                AssessmentPhase.IN_FLIGHT,
                envelope=request_envelope,
            )

        try:
            trace.assessor_started_at = now_ts()
            started_event = self._assessment_model_started.get(seq)
            if started_event is not None:
                started_event.set()
            decision = self._fast_scan_negative_decision(request=request)
            if decision is None:
                decision = await self._run_model_call(
                    lambda: self.assessor.assess(request=request),
                    component="assessor",
                    purpose="assessment",
                    priority=ModelPriority.ASSESSOR,
                    envelope=request_envelope,
                )
        except asyncio.CancelledError:
            if self._closed or self._finished:
                raise
            async with self._ledger_lock:
                await self._actor_discard_turn(owner.turn_id)
                self._release_assessment_owner(owner, assessment_task)
            await self._actor_set_assessor_status(
                "cancelled",
                envelope=request_envelope,
            )
            await self.session_actor.set_assessment_phase(
                AssessmentPhase.CANCELLED,
                envelope=request_envelope,
            )
            trace.stale = True
            raise
        except Exception as exc:
            if self._closed or self._finished:
                return ""
            trace.repair_required = True
            logger.exception(
                "SCID backend assess failed - episode: %s, turn: %s, field: %s",
                self.episode_id,
                owner.turn_id,
                source_field_id,
            )
            decision = fallback_reask_decision(
                field_id=source_field_id or "",
                reason=f"后台模型调用失败：{type(exc).__name__}: {exc}",
                question=(
                    "我刚才核对后台流程时遇到了一点技术问题。"
                    "我们先继续确认：你能再具体说说这种感觉出现时的情况吗？"
                ),
            )
        finally:
            if not self._closed and not self._finished:
                trace.assessor_finished_at = now_ts()
                if self.blackboard.assessor_status != "cancelled":
                    await self._actor_set_assessor_status(
                        "ready",
                        envelope=request_envelope,
                    )

        if self._closed or self._finished:
            return ""

        trace.assessor_action = decision.next_action
        if trace.observer_action is not None:
            trace.observer_assessor_agree = self._observer_assessor_agree(
                trace.observer_action,
                decision.next_action,
            )

        async with self._ledger_lock:
            current_owner = self._assessment_owners.get(owner.token)
            if (
                not self._assessment_result_is_applicable(
                    interaction_seq=seq,
                    source_field_id=source_field_id,
                    source_state_version=source_state_version,
                    allow_speculative_commit=allow_speculative_commit,
                )
                or current_owner is not owner
                or not owner.active
            ):
                turn.stale = True
                trace.stale = True
                await self._actor_discard_turn(owner.turn_id)
                self._release_assessment_owner(owner, assessment_task)
                await self._actor_set_assessor_status(
                    "stale",
                    envelope=request_envelope,
                )
                await self.session_actor.set_assessment_phase(
                    AssessmentPhase.REJECTED,
                    envelope=request_envelope,
                )
                return ""

            try:
                await self._actor_apply_decision(
                    decision=decision,
                    turn_id=owner.turn_id,
                    expected_field_id=source_field_id,
                    expected_state_version=source_state_version,
                    envelope=request_envelope,
                )
                trace.ledger_committed_at = now_ts()
            except LedgerValidationError as exc:
                if not self._assessment_result_is_applicable(
                    interaction_seq=seq,
                    source_field_id=source_field_id,
                    source_state_version=source_state_version,
                    allow_speculative_commit=allow_speculative_commit,
                ):
                    turn.stale = True
                    trace.stale = True
                    await self._actor_discard_turn(owner.turn_id)
                    self._release_assessment_owner(owner, assessment_task)
                    await self._actor_set_assessor_status(
                        "stale",
                        envelope=request_envelope,
                    )
                    await self.session_actor.set_assessment_phase(
                        AssessmentPhase.REJECTED,
                        envelope=request_envelope,
                    )
                    return ""
                trace.repair_required = True
                fallback = fallback_reask_decision(
                    field_id=self.ledger.current_field_id or "",
                    reason=f"后台决策未通过校验：{exc}",
                )
                trace.assessor_action = fallback.next_action
                if trace.observer_action is not None:
                    trace.observer_assessor_agree = self._observer_assessor_agree(
                        trace.observer_action,
                        fallback.next_action,
                    )
                try:
                    await self._actor_apply_decision(
                        decision=fallback,
                        turn_id=owner.turn_id,
                        expected_field_id=source_field_id,
                        expected_state_version=source_state_version,
                        envelope=request_envelope,
                    )
                    trace.ledger_committed_at = now_ts()
                except LedgerValidationError:
                    logger.exception(
                        "SCID fallback decision also failed - episode: %s, turn: %s",
                        self.episode_id,
                        owner.turn_id,
                    )
                    await self._actor_discard_turn(owner.turn_id)
                    self._release_assessment_owner(owner, assessment_task)
                    await self._actor_set_assessor_status(
                        "failed",
                        envelope=request_envelope,
                    )
                    await self.session_actor.set_assessment_phase(
                        AssessmentPhase.FAILED,
                        envelope=request_envelope,
                    )
                    failure_text = (
                        "我刚才核对流程时遇到了一点技术问题。我们先暂停一下。"
                    )
                    if not render_response:
                        return DialogueDirective(
                            directive_type="repair_prompt",
                            field_id=self.ledger.current_field_id,
                            question_text=failure_text,
                            instruction="直接说明需要暂停，不要透露内部字段或 JSON。",
                        )
                    return failure_text

            owner.committed = True
            self._release_assessment_owner(owner, assessment_task)
            await self._actor_set_assessor_status(
                "committed",
                envelope=request_envelope,
            )
            self.candidate_cache.invalidate_before(self.ledger.state_version)
            if (
                self.blackboard.repair_pending is not None
                and self.ledger.current_field_id != source_field_id
            ):
                if self.speculation_saga.phase is SpeculationPhase.COMPENSATING:
                    await self._transition_speculation(
                        SpeculationPhase.CLOSED,
                        reason="repair_answer_committed",
                        mutation=self.blackboard.clear_repair,
                        envelope=request_envelope,
                    )
                else:
                    await self.session_actor.call(
                        "clear_speculation_repair",
                        self.blackboard.clear_repair,
                        event_type="SpeculationRepairCleared",
                        event_payload={},
                        envelope=request_envelope,
                    )
            self.pending_user_buffer = ""
            await self.session_actor.call(
                "clear_foreground_probe_after_commit",
                self.blackboard.clear_foreground_probe,
                event_type="ForegroundProbeCleared",
                event_payload={},
                envelope=request_envelope,
            )
            archived = await self._actor_archive_ledger_turns()
            if archived:
                self._notify_history_archived("ledger_turns", archived)
            directive = self.ledger.get_directive()

        await self.session_actor.set_assessment_phase(
            AssessmentPhase.COMMITTED,
            envelope=request_envelope,
        )

        if self.ledger.terminal_status:
            self.interaction_mode = (
                "crisis" if self.ledger.terminal_status == "crisis" else "completed"
            )
            self._request_terminal_finalize(self.ledger.terminal_status)
            terminal_text = (
                CRISIS_RESPONSE_ZH
                if self.ledger.terminal_status == "crisis"
                else SESSION_COMPLETED_ZH
            )
            if not render_response:
                return DialogueDirective(
                    directive_type=(
                        "crisis"
                        if self.ledger.terminal_status == "crisis"
                        else "complete"
                    ),
                    field_id=None,
                    question_text=terminal_text,
                    instruction="输出版本化固定关键话术，不调用外部前台模型。",
                )
            return terminal_text
        if not render_response:
            return directive

        trace.frontend_followup_started_at = now_ts()
        final_text = await self.dialogue_model.render(directive)
        if self._closed or self._finished:
            return ""
        trace.frontend_followup_finished_at = now_ts()
        if not self.is_latest_interaction(
            seq
        ) and not self._assessment_result_is_applicable(
            interaction_seq=seq,
            source_field_id=source_field_id,
            source_state_version=source_state_version,
            allow_speculative_commit=allow_speculative_commit,
            after_commit=True,
        ):
            turn.stale = True
            trace.stale = True
            return ""

        return final_text

    def _assessment_result_is_applicable(
        self,
        *,
        interaction_seq: int,
        source_field_id: str | None,
        source_state_version: int,
        allow_speculative_commit: bool,
        after_commit: bool = False,
    ) -> bool:
        if self._closed or self._finished:
            return False
        result_generation = self._generation_by_seq.get(interaction_seq)
        current_generation = self.session_actor.ingress_generation
        if self.is_latest_interaction(interaction_seq):
            return result_generation == current_generation
        if not allow_speculative_commit or self.interaction_mode in {
            "crisis",
            "completed",
        }:
            return False
        state = self.blackboard.speculative_advance
        if state is None:
            return False
        if (
            state.source_interaction_seq != interaction_seq
            or state.source_field_id != source_field_id
            or state.based_on_state_version != source_state_version
        ):
            return False
        if self.speculation_saga.source_interaction_seq not in {
            None,
            interaction_seq,
        }:
            return False
        if after_commit:
            return self.ledger.state_version >= source_state_version + 1
        return (
            self.ledger.current_field_id == source_field_id
            and self.ledger.state_version == source_state_version
        )

    @staticmethod
    def _observer_assessor_agree(
        observer_action: str,
        assessor_action: str,
    ) -> bool | None:
        if observer_action == "hold_for_assessor":
            return None
        if observer_action == "ask_next_field":
            return assessor_action in {"advance", "branch"}
        if observer_action == "request_safety_review":
            return assessor_action == "crisis"
        return assessor_action in {"clarify", "reask"}

    async def _apply_crisis(
        self,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
    ) -> str:
        if self._closed or self._finished:
            return ""
        self.interaction_mode = "crisis"
        self.pending_user_buffer = ""
        await self.session_actor.call(
            "clear_foreground_probe_for_crisis",
            self.blackboard.clear_foreground_probe,
            event_type="ForegroundProbeCleared",
            event_payload={"reason": "crisis"},
            envelope=self._causal_envelope(turn.interaction_seq),
        )
        await self.session_actor.set_session_phase(
            SessionPhase.CRISIS,
            event_type="CrisisTriggered",
            envelope=self._causal_envelope(turn.interaction_seq),
        )
        async with self._ledger_lock:
            if self._closed or self._finished:
                return ""
            await self._discard_inactive_assessment_turns()
            if self.ledger.terminal_status != "crisis":
                crisis_envelope = self._causal_envelope(turn.interaction_seq)
                ledger_turn = await self._actor_begin_turn(
                    user_text=route.normalized_user_text or turn.user_text,
                    interaction_seq=turn.interaction_seq,
                    envelope=crisis_envelope,
                )
                turn.scid_turn_id = ledger_turn.turn_id
                await self.session_actor.set_assessment_phase(
                    AssessmentPhase.IN_FLIGHT,
                    envelope=crisis_envelope,
                )
                decision = fallback_reask_decision(
                    field_id=self.ledger.current_field_id or "",
                    reason=route.reasoning_summary or "Safety route interrupted SCID.",
                )
                decision.next_action = "crisis"
                decision.evidence = [turn.user_text]
                await self._actor_apply_decision(
                    decision=decision,
                    turn_id=ledger_turn.turn_id,
                    expected_field_id=ledger_turn.field_id,
                    expected_state_version=self.ledger.state_version,
                    envelope=crisis_envelope,
                )
                await self.session_actor.set_assessment_phase(
                    AssessmentPhase.COMMITTED,
                    envelope=crisis_envelope,
                )
                self.candidate_cache.clear()
            directive = self.ledger.get_directive()

        del directive
        self._request_terminal_finalize("crisis")
        return CRISIS_RESPONSE_ZH

    async def _route_text(
        self, user_text: str, interaction_seq: int
    ) -> SCIDRouteDecision:
        context = self._build_router_context(user_text, interaction_seq)
        trace = self._trace_for(interaction_seq)
        trace.pre_router_started_at = now_ts()
        try:
            envelope = self._causal_envelope(interaction_seq)
            route = await self._run_model_call(
                lambda: self.router.route(context=context),
                component="router",
                purpose="route",
                priority=ModelPriority.CRISIS_OR_FOREGROUND,
                envelope=envelope,
            )
        finally:
            if not self._closed and not self._finished:
                trace.pre_router_finished_at = now_ts()
        if self._closed or self._finished:
            return route
        trace.route = route.route
        logger.info(
            "SCID control route - episode: %s, seq: %s, route: %s",
            self.episode_id,
            interaction_seq,
            route.route,
        )
        return route

    async def _route_text_realtime(
        self,
        user_text: str,
        interaction_seq: int,
    ) -> SCIDRouteDecision:
        """Run only the local control-plane route for realtime mode."""

        context = self._build_router_context(user_text, interaction_seq)
        trace = self._trace_for(interaction_seq)
        trace.pre_router_started_at = now_ts()
        call_started = monotonic_ts()
        try:
            route = await self.control_router.route(context=context)
        finally:
            if not self._closed and not self._finished:
                trace.pre_router_finished_at = now_ts()
        if self._closed or self._finished:
            return route
        if not self.session_actor.closed:
            await self.session_actor.emit(
                "ModelCallCompleted",
                payload={
                    "component": "router",
                    "purpose": "realtime_control_route",
                    "priority": "CRISIS_OR_FOREGROUND",
                    "status": "completed",
                    "total_latency_ms": round(
                        (monotonic_ts() - call_started) * 1000, 3
                    ),
                    "result_summary": _audit_model_result_summary(route),
                },
                envelope=self._causal_envelope(interaction_seq),
            )
        trace.route = route.route
        logger.info(
            "SCID realtime control route - episode: %s, seq: %s, route: %s",
            self.episode_id,
            interaction_seq,
            route.route,
        )
        return route

    def _build_router_context(
        self,
        user_text: str,
        interaction_seq: int,
    ) -> dict[str, Any]:
        field = self.ledger.current_field
        return {
            "interaction_seq": interaction_seq,
            "interaction_mode": self.interaction_mode,
            "user_text": user_text,
            "pending_user_buffer": self.pending_user_buffer,
            "current_node": field.snapshot() if field else None,
            "current_directive": self.ledger.get_directive().snapshot(),
            "progress": self._progress_snapshot(),
            "filled_field_count": len(self.ledger.field_states),
            "recent_interactions": [
                item.snapshot() for item in self.interaction_turns[-6:]
            ],
        }

    def _progress_snapshot(self) -> dict[str, Any]:
        current_field_id = self.ledger.current_field_id
        if current_field_id in self.template.scan_order:
            return {
                "phase": "scan",
                "current": self.template.scan_order.index(current_field_id) + 1,
                "total": len(self.template.scan_order),
            }
        return {
            "phase": "priority_module" if current_field_id else "terminal",
            "current_field_id": current_field_id,
            "total": len(self.template.scan_order),
        }

    def _resume_directive(self, route: SCIDRouteDecision) -> DialogueDirective:
        current = self.ledger.get_directive()
        return DialogueDirective(
            directive_type="resume_prompt",
            field_id=current.field_id,
            question_text=current.question_text,
            instruction=("简短确认继续，然后自然问出当前问题。不要评分，不要诊断。"),
            progress_text=current.progress_text,
            safety_note=current.safety_note,
            allowed_actions=["ask", "clarify_wording", "respect_stop"],
        )

    def _scoring_text_for(self, route: SCIDRouteDecision, user_text: str) -> str:
        normalized = route.normalized_user_text.strip()
        if self.pending_user_buffer:
            return self._combine_pending(
                self.pending_user_buffer, normalized or user_text
            )
        return normalized or user_text

    def _combine_pending(self, pending: str, text: str) -> str:
        pending = pending.strip()
        text = text.strip()
        if not pending:
            return text
        if text == pending:
            return pending
        if not text:
            return pending
        return f"{pending}\n{text}"

    def _record_interaction(
        self,
        interaction_seq: int,
        user_text: str,
        *,
        route_decision: SCIDRouteDecision | None = None,
        ignored_reason: str = "",
    ) -> SCIDInteractionTurn:
        turn = SCIDInteractionTurn(
            interaction_seq=interaction_seq,
            user_text=user_text,
            interaction_mode=self.interaction_mode,
            route_decision=route_decision.snapshot() if route_decision else None,
            ignored_reason=ignored_reason,
        )
        self.interaction_turns.append(turn)
        if route_decision is not None:
            self.session_memory.observe_route(route_decision.route)
        limit = self.runtime_policy.retention.recent_interaction_turns
        if len(self.interaction_turns) > limit:
            archived = len(self.interaction_turns) - limit
            del self.interaction_turns[:archived]
            self._notify_history_archived("interaction_turns", archived)
        return turn

    def _final_response(
        self,
        turn: SCIDInteractionTurn,
        final_text: str,
    ) -> _RouteResponse:
        if self._closed or self._finished:
            return _RouteResponse(
                final_text="",
                stale=True,
                interaction_seq=turn.interaction_seq,
            )
        if turn.stale:
            final_text = ""
        turn.assistant_text = final_text
        return _RouteResponse(
            final_text=final_text,
            stale=turn.stale,
            interaction_seq=turn.interaction_seq,
        )

    def _trace_for(self, interaction_seq: int) -> SCIDLatencyTrace:
        trace = self.latency_traces.get(interaction_seq)
        if trace is None:
            trace = SCIDLatencyTrace(
                interaction_seq=interaction_seq,
                field_id=self.ledger.current_field_id,
            )
            self.latency_traces[interaction_seq] = trace
            limit = self.runtime_policy.retention.recent_latency_traces
            if len(self.latency_traces) > limit:
                archived_keys = sorted(self.latency_traces)[:-limit]
                for key in archived_keys:
                    self.latency_traces.pop(key, None)
                self._notify_history_archived(
                    "latency_traces",
                    len(archived_keys),
                )
        if trace.field_id is None:
            trace.field_id = self.ledger.current_field_id
        return trace

    def _record_runtime_turn(
        self,
        *,
        interaction_seq: int,
        route: str,
        initial_directive: DialogueDirective | None,
    ) -> dict[str, Any]:
        record = {
            "interaction_seq": interaction_seq,
            "route": route,
            "initial_directive": (
                initial_directive.snapshot() if initial_directive is not None else None
            ),
            "initial_text": "",
            "action_text": "",
            "action_stale": False,
            "action_delivery_status": "not_selected",
            "response_delivery_status": "pending",
            "repair_required": False,
        }
        self.runtime_turns.append(record)
        limit = self.runtime_policy.retention.recent_runtime_turns
        if len(self.runtime_turns) > limit:
            archived = len(self.runtime_turns) - limit
            del self.runtime_turns[:archived]
            self._notify_history_archived("runtime_turns", archived)
        return record

    def _record_foreground_action(self, action: ForegroundAction) -> None:
        payload = action.snapshot()
        payload["selected"] = True
        payload["delivery_started"] = False
        self.foreground_actions.append(payload)
        limit = self.runtime_policy.retention.recent_foreground_actions
        if len(self.foreground_actions) > limit:
            del self.foreground_actions[:-limit]

    def _notify_history_archived(self, collection: str, count: int) -> None:
        if count <= 0 or self.session_actor.closed:
            return

        def mutation() -> None:
            attribute = {
                "interaction_turns": "archived_interactions",
                "runtime_turns": "archived_runtime_turns",
                "latency_traces": "archived_latency_traces",
                "ledger_turns": "archived_ledger_turns",
            }.get(collection)
            if attribute is not None:
                setattr(
                    self.session_actor.state,
                    attribute,
                    getattr(self.session_actor.state, attribute) + count,
                )

        self._notify_actor(
            self.session_actor.call(
                "archive_history",
                mutation,
                lane=MailboxLane.CONTROL,
                event_type="HistoryArchived",
                event_payload={"collection": collection, "count": count},
            )
        )

    def _runtime_record_for(self, interaction_seq: int) -> dict[str, Any] | None:
        return next(
            (
                record
                for record in reversed(self.runtime_turns)
                if record.get("interaction_seq") == interaction_seq
            ),
            None,
        )

    @staticmethod
    def _is_obvious_asr_noise(text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return True
        return not any(char.isalnum() for char in stripped)

    def finish(self, *, status: str | None = None) -> Path:
        """Finalize a quiescent episode immediately.

        Product runtime paths use terminal-pending finalization after response
        delivery. This compatibility method remains for explicit local aborts.
        """

        return self._finalize_episode(status=status or self.status)

    def _request_terminal_finalize(self, status: str) -> None:
        self.status = status
        self._terminal_pending_status = status

    def complete_response_delivery(
        self,
        interaction_seq: int,
        *,
        success: bool,
    ) -> Path | None:
        """Record delivery completion and finalize a pending terminal episode."""

        _validate_interaction_seq(interaction_seq)
        if self._closed or self._finished:
            return self._episode_path
        success = (
            success and not self._trace_for(interaction_seq).frontend_stream_truncated
        )
        if not self._record_delivery_completion(interaction_seq, success=success):
            return self._episode_path
        if self.persist_raw_transcript:
            self._notify_actor(self._persist_delivery_artifacts(interaction_seq))
        if (
            not success
            and self.speculation_saga.source_interaction_seq == interaction_seq
        ):
            self._notify_actor(self._cancel_speculation(reason="delivery_failed"))
        if self._terminal_pending_status is not None and not self._finished:
            return self._finalize_episode(status=self._terminal_pending_status)
        self._save_partial_snapshot()
        return None

    async def _persist_delivery_artifacts(self, interaction_seq: int) -> None:
        """Write opted-in assistant text to the separate artifact stream."""

        if not self.persist_raw_transcript:
            return
        record = self._runtime_record_for(interaction_seq)
        if record is None:
            return
        seen: set[str] = set()
        for key in ("initial_text", "bridge_text", "action_text"):
            text = record.get(key)
            if not isinstance(text, str) or not text or text in seen:
                continue
            seen.add(text)
            await self.event_store.append_artifact(
                interaction_seq=interaction_seq,
                role="assistant",
                text=text,
            )

    def _record_delivery_completion(
        self,
        interaction_seq: int,
        *,
        success: bool,
    ) -> bool:
        """Apply an idempotent in-memory delivery receipt."""

        if interaction_seq in self._delivery_receipts:
            return False
        self._delivery_receipts[interaction_seq] = bool(success)
        receipt_limit = self.runtime_policy.retention.recent_latency_traces
        if len(self._delivery_receipts) > receipt_limit:
            for seq in sorted(self._delivery_receipts)[:-receipt_limit]:
                self._delivery_receipts.pop(seq, None)
        trace = self._trace_for(interaction_seq)
        success = success and not trace.frontend_stream_truncated
        record = self._runtime_record_for(interaction_seq)
        if record is not None:
            record["response_delivery_status"] = (
                "delivery_complete" if success else "delivery_failed"
            )
        if record is not None and record.get("action_delivery_status") in {
            "selected",
            "delivery_started",
        }:
            record["action_delivery_status"] = (
                "delivery_complete" if success else "delivery_failed"
            )
        if trace.action_delivery_status in {"selected", "delivery_started"}:
            trace.action_delivery_status = (
                "delivery_complete" if success else "delivery_failed"
            )
        if record is not None:
            selected = record.get("selected_action")
            action_id = (
                selected.get("action_id") if isinstance(selected, dict) else None
            )
            for action in reversed(self.foreground_actions):
                if action.get("action_id") == action_id:
                    action["delivery_completed"] = bool(success)
                    action["delivery_failed"] = not success
                    break
        self._complete_interaction_seq(interaction_seq)
        return True

    async def acomplete_response_delivery(
        self,
        interaction_seq: int,
        *,
        success: bool,
    ) -> Path | None:
        """Record a delivery receipt and durably finish terminal sessions.

        Parameters
        ----------
        interaction_seq : int
            Interaction whose combined initial/follow-up stream was delivered.
        success : bool
            Whether downstream consumption completed without truncation.

        Returns
        -------
        pathlib.Path | None
            Final episode path for a terminal interaction, otherwise ``None``.
        """

        _validate_interaction_seq(interaction_seq)
        if self._closed or self._finished:
            return self._episode_path
        success = (
            success and not self._trace_for(interaction_seq).frontend_stream_truncated
        )
        if not self._record_delivery_completion(interaction_seq, success=success):
            return self._episode_path
        await self._persist_delivery_artifacts(interaction_seq)
        if (
            not success
            and self.speculation_saga.source_interaction_seq == interaction_seq
        ):
            await self._cancel_speculation(reason="delivery_failed")
        delivery_phase = DeliveryPhase.COMPLETED if success else DeliveryPhase.FAILED
        if not self.session_actor.closed:
            await self.session_actor.set_delivery_phase(
                delivery_phase,
                envelope=self._causal_envelope(interaction_seq),
            )
            await self.session_actor.emit(
                "DeliveryCompleted",
                payload={"success": bool(success)},
                envelope=self._causal_envelope(interaction_seq),
            )
            await self.session_actor.emit(
                "ForegroundSegmentDeliveryRecorded",
                payload={
                    "delivery_status": (
                        "delivery_complete" if success else "delivery_failed"
                    )
                },
                envelope=self._causal_envelope(interaction_seq),
            )
        supervisor = self._turn_supervisors.pop(interaction_seq, None)
        if supervisor is not None and supervisor.active_count == 0:
            await supervisor.close()
        elif supervisor is not None:
            self._turn_supervisors[interaction_seq] = supervisor
        if self._terminal_pending_status is not None and not self._finished:
            return await self._afinalize_episode(status=self._terminal_pending_status)
        self._save_partial_snapshot()
        return None

    async def release_turn(self, interaction_seq: int) -> None:
        """Release a completed turn scope after its stream has been consumed."""

        _validate_interaction_seq(interaction_seq)
        supervisor = self._turn_supervisors.pop(interaction_seq, None)
        if supervisor is not None:
            await supervisor.close()

    async def _afinalize_episode(self, *, status: str) -> Path:
        """Drain writers and durably persist one terminal episode projection."""

        if self._episode_path is not None:
            self._finished = True
            return self._episode_path
        previous_status = self.status
        previous_ended_at = self.ended_at
        final_ended_at = utc_now_iso()
        self.status = status
        self.ended_at = final_ended_at
        phase = {
            "crisis": SessionPhase.CRISIS,
            "completed": SessionPhase.COMPLETED,
            "stopped": SessionPhase.STOPPED,
            "aborted": SessionPhase.ABORTED,
        }.get(status, SessionPhase.ABORTED)
        path = self.episode_dir / f"{self.episode_id}.json"
        try:
            if self._started and not self.session_actor.closed:
                await self.session_actor.close(phase=phase)
            elif not self.event_store.closed:
                await self.event_store.close()
            payload = self.snapshot()
            payload["ended_at"] = final_ended_at
            await self.event_store.write_final(path=path, payload=payload)
        except Exception:
            self.status = previous_status
            self.ended_at = previous_ended_at
            raise
        self._episode_path = path
        self._finished = True
        self._terminal_pending_status = None
        with suppress(FileNotFoundError):
            self.partial_episode_path.unlink()
        return path

    def _finalize_episode(self, *, status: str) -> Path:
        """Atomically write the latest terminal snapshot and remove its partial."""

        if self._episode_path is not None:
            self._finished = True
            return self._episode_path
        previous_status = self.status
        previous_ended_at = self.ended_at
        final_ended_at = utc_now_iso()
        self.status = status
        self.ended_at = final_ended_at
        path = self.episode_dir / f"{self.episode_id}.json"
        try:
            payload = self.snapshot()
            payload["ended_at"] = final_ended_at
            self._atomic_write_snapshot(path, payload)
        except Exception:
            self.status = previous_status
            self.ended_at = previous_ended_at
            raise
        self._episode_path = path
        self._finished = True
        self._terminal_pending_status = None
        try:
            self.partial_episode_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "SCID final snapshot committed but partial cleanup failed - "
                "episode: %s, error_type: %s",
                self.episode_id,
                type(exc).__name__,
            )
        return path

    def snapshot(self) -> dict[str, Any]:
        """Return the configured privacy-preserving episode projection."""

        finalized = self.ended_at is not None
        raw_snapshot = {
            "snapshot_schema_version": 4,
            "episode_id": self.episode_id,
            "user_id_hash": hashlib.sha256(self.user_id.encode("utf-8")).hexdigest(),
            "task": "scid_voice_assessment_v1",
            "product_contract_version": PRODUCT_CONTRACT_VERSION,
            "deployment_scope": DEPLOYMENT_SCOPE,
            "intended_use": "non_diagnostic_structured_support",
            "experiment_id": self.experiment_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "runtime_profile": "realtime_v3",
            "runtime_policy": self.runtime_policy.snapshot(),
            "raw_transcript_persisted": self.persist_raw_transcript,
            "backend_model": self.backend_model,
            "observer_model": self.observer_model,
            "observer_mode": self.observer_mode,
            "one_step_speculation_enabled": self.allow_one_step_speculation,
            "partial_plan_max_age_seconds": self.partial_plan_max_age_seconds,
            "post_initial_action_wait_seconds": (self.post_initial_action_wait_seconds),
            "interaction_mode": self.interaction_mode,
            "pending_user_buffer": self.pending_user_buffer,
            "active_task_state": {
                "latest_interaction_seq": self._latest_interaction_seq,
                "sequence_states": dict(self._sequence_states),
                "ledger_lock_locked": (
                    False if finalized else self._ledger_lock.locked()
                ),
                "observer_task_count": (0 if finalized else len(self._observer_tasks)),
                "assessment_task_count": (
                    0 if finalized else len(self._assessment_tasks)
                ),
                "action_task_count": (0 if finalized else len(self._action_tasks)),
                "foreground_worker_task_count": (
                    0 if finalized else len(self._foreground_worker_tasks)
                ),
                "speculative_watcher_count": (
                    0 if finalized else len(self._speculative_watcher_tasks)
                ),
                "turn_supervisor_count": (
                    0 if finalized else len(self._turn_supervisors)
                ),
            },
            "actor_state": self.session_actor.snapshot(),
            "event_log": self.event_store.snapshot(),
            "model_gateway": self.model_gateway.snapshot(),
            "session_memory": self.session_memory.snapshot(),
            "speculation_saga": self.speculation_saga.snapshot(),
            "route_decisions": [
                turn.route_decision
                for turn in self.interaction_turns
                if turn.route_decision is not None
            ],
            "interaction_turns": [turn.snapshot() for turn in self.interaction_turns],
            "runtime_turns": list(self.runtime_turns),
            "foreground_actions": list(self.foreground_actions),
            "clinical_blackboard": self.blackboard.snapshot(),
            "candidate_utterance_cache": self.candidate_cache.snapshot(),
            "latency_traces": [
                trace.snapshot() for _, trace in sorted(self.latency_traces.items())
            ],
            "lifecycle_events": list(self._lifecycle_events),
            "ledger": self.ledger.snapshot(),
        }
        return _episode_projection(
            raw_snapshot,
            persist_text=self.persist_raw_transcript,
        )

    def current_directive(self) -> DialogueDirective:
        """Return the current directive for tests/tools."""

        return self.ledger.get_directive()

    async def read_events(
        self,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read a page of redacted domain events.

        Parameters
        ----------
        after_seq : int, optional
            Return events with a sequence greater than this value.
        limit : int, optional
            Maximum number of events, between 1 and 500.

        Returns
        -------
        list[dict[str, Any]]
            Events ordered by their monotonic session sequence.
        """

        if limit > self.runtime_policy.event_page_maximum:
            raise ValueError("limit exceeds the configured event page maximum")
        return await self.event_store.read_events(
            after_seq=after_seq,
            limit=limit,
        )

    @property
    def partial_episode_path(self) -> Path:
        """Return the path used for the live, non-terminal debug snapshot."""

        return self.episode_dir / f"{self.episode_id}.partial.json"

    def _save_partial_snapshot(self) -> None:
        """Offer the latest in-progress projection to the async writer."""

        if self._finished:
            return
        try:
            self.event_store.request_snapshot(self.snapshot())
        except Exception as exc:
            logger.warning(
                "Failed to save SCID partial snapshot - episode: %s, error: %s",
                self.episode_id,
                exc,
            )

    def _atomic_write_snapshot(self, path: Path, payload: dict[str, Any]) -> None:
        """Write JSON with private permissions and atomic replacement."""

        with self._write_lock:
            self.episode_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.episode_dir, 0o700)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self.episode_id}.",
                suffix=".tmp",
                dir=self.episode_dir,
            )
            temporary_path = Path(temporary_name)
            replaced = False
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, path)
                replaced = True
                try:
                    directory_fd = os.open(self.episode_dir, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError as exc:
                    # Replacement is already atomic and the file itself was
                    # fsynced.  Report directory durability uncertainty without
                    # rolling in-memory state back behind a committed file.
                    logger.warning(
                        "SCID snapshot directory fsync failed after replace - "
                        "path: %s, error_type: %s",
                        path,
                        type(exc).__name__,
                    )
            except Exception:
                with suppress(OSError):
                    os.close(fd)
                if not replaced:
                    with suppress(FileNotFoundError):
                        temporary_path.unlink()
                raise


_SENSITIVE_TEXT_KEYS = {
    "action_text",
    "assistant_text",
    "candidate_texts",
    "clarification_question",
    "content",
    "deferred_user_text",
    "evidence",
    "error",
    "frontend_stream_error",
    "initial_text",
    "instruction",
    "latest_user_text",
    "normalized_user_text",
    "partial_text",
    "pending_clarification",
    "pending_user_buffer",
    "progress_text",
    "question_text",
    "quote",
    "raw_user_text",
    "reason",
    "reasoning_summary",
    "safe_frontend_content",
    "safety_note",
    "source_user_text",
    "stable_partial_text",
    "text",
    "user_text",
}


def _episode_projection(
    payload: dict[str, Any],
    *,
    persist_text: bool,
) -> dict[str, Any]:
    def artifact_ref(text: str) -> str:
        return f"text_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"

    def project(value: Any) -> Any:
        if isinstance(value, dict):
            projected: dict[str, Any] = {}
            for key, item in value.items():
                if key == "raw_payload":
                    continue
                if key in _SENSITIVE_TEXT_KEYS:
                    continue
                projected[key] = project(item)
            return projected
        if isinstance(value, list):
            return [project(item) for item in value]
        if isinstance(value, tuple):
            return [project(item) for item in value]
        return value

    projected = project(copy.deepcopy(payload))
    projected["text_artifacts"] = []
    transcript: list[dict[str, Any]] = []
    if persist_text:
        runtime_by_seq = {
            record.get("interaction_seq"): record
            for record in payload.get("runtime_turns", [])
        }
        for turn in payload.get("interaction_turns", []):
            seq = turn.get("interaction_seq")
            user_text = turn.get("user_text")
            if isinstance(user_text, str) and user_text:
                transcript.append(
                    {
                        "interaction_seq": seq,
                        "role": "user",
                        "text_ref": artifact_ref(user_text),
                    }
                )
            runtime_record = runtime_by_seq.get(seq)
            assistant_parts: list[str] = []
            if isinstance(runtime_record, dict):
                for key in ("initial_text", "action_text"):
                    part = runtime_record.get(key)
                    if isinstance(part, str) and part and part not in assistant_parts:
                        assistant_parts.append(part)
            if not assistant_parts:
                assistant_text = turn.get("assistant_text")
                if isinstance(assistant_text, str) and assistant_text:
                    assistant_parts.append(assistant_text)
            for part in assistant_parts:
                transcript.append(
                    {
                        "interaction_seq": seq,
                        "role": "assistant",
                        "text_ref": artifact_ref(part),
                    }
                )
    projected["transcript"] = transcript
    return projected


def _strict_runtime_bool(name: str, value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a JSON boolean")
    return value


def _strict_runtime_string(
    name: str,
    value: Any,
    *,
    maximum_length: int = 2048,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    parsed = value.strip()
    if len(parsed) > maximum_length:
        raise ValueError(f"{name} must be at most {maximum_length} characters")
    return parsed


def _new_episode_id() -> str:
    """Return a sortable, human-readable episode identifier.

    The timestamp makes adjacent files easy to correlate with service logs;
    the short random suffix prevents collisions without exposing a user or
    session identifier.
    """

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"scid_{timestamp}_{uuid4().hex[:8]}"


def _strict_episode_id(value: Any) -> str:
    parsed = _strict_runtime_string("episode_id", value, maximum_length=128)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", parsed):
        raise ValueError(
            "episode_id may contain only letters, digits, dot, underscore, and dash"
        )
    return parsed


def _strict_runtime_float(
    name: str,
    value: Any,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _validate_interaction_seq(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("interaction_seq must be a positive integer")


def _strict_optional_timestamp(name: str, value: Any | None) -> float:
    if value is None:
        return now_ts()
    return _strict_runtime_float(
        name,
        value,
        minimum=0.0,
        maximum=float("1e20"),
    )


def _strict_asr_rejection_reason(value: Any) -> str:
    allowed = {"empty_after_normalization", "input_too_long"}
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("reason must be empty_after_normalization or input_too_long")
    return value


def _normalize_input_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFC", value)
    cleaned = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char
        for char in normalized
    )
    return " ".join(cleaned.split())


def _audit_model_result_summary(result: Any) -> dict[str, Any]:
    """Return only stable structured conclusions, never model text/payloads."""

    summary: dict[str, Any] = {}
    for source, target in (
        ("route", "route"),
        ("next_action", "next_action"),
        ("recommended_action", "recommended_action"),
        ("confidence", "confidence"),
        ("stale", "stale"),
    ):
        value = getattr(result, source, None)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if value is not None:
                summary[target] = value
    if not summary and isinstance(result, dict):
        for key in ("route", "next_action", "recommended_action", "confidence"):
            value = result.get(key)
            if isinstance(value, (str, int, float, bool)):
                summary[key] = value
    return summary


def _audit_segment_type(
    phase: str,
    record: dict[str, Any] | None,
) -> str:
    if phase == "initial":
        return "initial_ack"
    if record is not None and record.get("bridge_source"):
        return "bridge"
    return "assessor_action"


def _audit_selected_action_id(record: dict[str, Any] | None) -> str | None:
    selected = record.get("selected_action") if record is not None else None
    if not isinstance(selected, dict):
        return None
    value = selected.get("action_id")
    return value if isinstance(value, str) else None
