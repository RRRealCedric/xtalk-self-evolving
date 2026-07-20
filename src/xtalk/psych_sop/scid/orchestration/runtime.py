"""Orchestrator runtime for the SCID dual-LM voice assessment."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, AsyncIterator, Literal, overload
from uuid import uuid4

from ....log_utils import logger
from ...episode_logger import DEFAULT_EPISODE_DIR
from ...safety_guard import SafetyGuard
from ..assessment.backend import BackgroundAssessor, create_background_assessor
from ..assessment.decision import fallback_reask_decision
from ..dialogue.candidate_cache import CandidateUtteranceCache
from ..dialogue.frontend import DialogueModel, RuleBasedDialogueModel
from ..dialogue.foreground import (
    FastForegroundPolicy,
    ForegroundAction,
    ForegroundActionBroker,
)
from ..dialogue.repair import RepairRequest, build_repair_directive
from ..policy.latency_controller import ClinicalLatencyController
from ..policy.observer import IncrementalObserver, create_incremental_observer
from ..policy.router import (
    RuleBasedSCIDInteractionRouter,
    SCIDInteractionRouter,
    create_scid_interaction_router,
)
from ..core.schema import (
    DialogueDirective,
    SCIDInteractionTurn,
    SCIDRouteDecision,
    TurnInterpretation,
    utc_now_iso,
)
from ..core.template import load_scid_template
from ..state.blackboard import ClinicalBlackboard, PartialObserverPlan
from ..state.ledger import AssessmentLedger, LedgerValidationError
from ..state.telemetry import SCIDLatencyTrace, now_ts


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


@dataclass(slots=True)
class SCIDRuntimeResponse:
    """Texts emitted for one accepted user interaction."""

    wait_text: str | None
    final_text: str
    stale: bool = False
    interaction_seq: int | None = None


@dataclass(slots=True)
class SCIDProgressiveRuntimeResponse:
    """Progressive texts emitted for one accepted user interaction."""

    interaction_seq: int
    initial_text: str
    followup_task: asyncio.Task[str] | None
    stale: bool = False
    terminal: bool = False


@dataclass(slots=True)
class SCIDRealtimeRuntimeResponse:
    """Realtime streams emitted for one accepted user interaction."""

    interaction_seq: int
    initial_stream: AsyncIterator[str] | None
    action_stream_task: asyncio.Task[AsyncIterator[str] | None] | None
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
        enable_wait_text: bool = True,
        runtime_mode: str = "sequential",
        observer_mode: str = "shadow",
        enable_candidate_pregeneration: bool = True,
        enable_optimistic_scan: bool = False,
        observer_confidence_threshold: float = 0.9,
        frontend_initial_timeout_seconds: float = 1.2,
        frontend_streaming: bool = True,
        fast_policy_enabled: bool = True,
        realtime_action_timeout_seconds: float = 8.0,
        realtime_action_grace_seconds: float = 1.0,
        realtime_observer_planning_enabled: bool = True,
        partial_plan_max_age_seconds: float = 3.0,
        post_initial_action_wait_seconds: float = 0.35,
    ) -> None:
        self.experiment_id = experiment_id
        self.user_id = user_id
        self.episode_dir = Path(episode_dir)
        self.backend_model = backend_model
        self.observer_model = observer_model or "deepseek-v4-flash"
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
        self.enable_wait_text = enable_wait_text
        self.runtime_mode = (
            runtime_mode
            if runtime_mode in {"sequential", "parallel", "realtime"}
            else "sequential"
        )
        self.observer_mode = (
            observer_mode if observer_mode in {"off", "shadow", "active"} else "shadow"
        )
        if (
            self.runtime_mode == "realtime"
            and self.observer_mode == "active"
            and self.observer_model == self.backend_model
        ):
            raise ValueError(
                "Active realtime Observer model must differ from the Assessor model"
            )
        self.enable_candidate_pregeneration = enable_candidate_pregeneration
        self.enable_optimistic_scan = (
            enable_optimistic_scan and self.observer_mode == "active"
        )
        self.frontend_initial_timeout_seconds = max(
            0.0,
            frontend_initial_timeout_seconds,
        )
        self.frontend_streaming = frontend_streaming
        self.realtime_action_timeout_seconds = max(
            0.1,
            realtime_action_timeout_seconds,
        )
        self.realtime_action_grace_seconds = max(
            0.0,
            realtime_action_grace_seconds,
        )
        self.realtime_observer_planning_enabled = bool(
            realtime_observer_planning_enabled
        )
        self.partial_plan_max_age_seconds = max(0.0, partial_plan_max_age_seconds)
        self.post_initial_action_wait_seconds = max(
            0.0,
            post_initial_action_wait_seconds,
        )
        self.fast_policy = FastForegroundPolicy(enabled=fast_policy_enabled)
        self.blackboard = ClinicalBlackboard(
            committed_state_version=self.ledger.state_version,
            current_field_id=self.ledger.current_field_id,
        )
        self.candidate_cache = CandidateUtteranceCache(self.dialogue_model)
        self.latency_controller = ClinicalLatencyController(
            observer_confidence_threshold=observer_confidence_threshold
        )

        self.episode_id = str(uuid4())
        self.started_at = utc_now_iso()
        self.ended_at: str | None = None
        self.status = "in_progress"
        self.interaction_mode = "scid"
        self.pending_user_buffer = ""
        self.interaction_turns: list[SCIDInteractionTurn] = []
        self.latency_traces: dict[int, SCIDLatencyTrace] = {}
        self.progressive_turns: list[dict[str, Any]] = []
        self.realtime_turns: list[dict[str, Any]] = []
        self.foreground_actions: list[dict[str, Any]] = []

        self._started = False
        self._finished = False
        self._episode_path: Path | None = None
        self._latest_interaction_seq = 0
        self._ledger_lock = asyncio.Lock()
        self._observer_tasks: set[asyncio.Task[TurnInterpretation]] = set()
        self._candidate_tasks: set[asyncio.Task[dict[str, str]]] = set()
        self._assessment_tasks: set[asyncio.Task[Any]] = set()
        self._speculative_watcher_tasks: set[asyncio.Task[None]] = set()
        self._pending_speculative_assessment: asyncio.Task[Any] | None = None

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

        trace = self._trace_for(interaction_seq)
        if trace.asr_partial_first_at is None:
            trace.asr_partial_first_at = timestamp or now_ts()
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

        self._claim_or_update_interaction_seq(interaction_seq)
        self._trace_for(interaction_seq).asr_final_at = timestamp or now_ts()

    def mark_first_segment_published(
        self,
        interaction_seq: int,
        *,
        timestamp: float | None = None,
    ) -> None:
        """Record when the first progressive segment was published."""

        self._trace_for(interaction_seq).first_segment_published_at = (
            timestamp or now_ts()
        )
        self._save_partial_snapshot()

    def mark_followup_segment_published(
        self,
        interaction_seq: int,
        *,
        timestamp: float | None = None,
    ) -> None:
        """Record when the progressive followup segment was published."""

        self._trace_for(interaction_seq).followup_segment_published_at = (
            timestamp or now_ts()
        )
        self._save_partial_snapshot()

    def mark_interaction_stale(self, interaction_seq: int) -> None:
        """Record that an interaction produced a stale async result."""

        self._trace_for(interaction_seq).stale = True
        self._save_partial_snapshot()

    async def observe_asr_partial(
        self,
        user_text: str,
        *,
        interaction_seq: int,
    ) -> TurnInterpretation | None:
        """Observe a stable ASR partial without scoring or foreground output."""

        if self.observer_mode == "off" or not user_text.strip() or self._finished:
            return None
        self.blackboard.stable_partial_text = user_text.strip()
        task = self._start_observer_task(
            user_text=user_text.strip(),
            interaction_seq=interaction_seq,
            input_kind="partial",
            interaction_turn=None,
        )
        if task is None:
            return None
        return await task

    def cancel_background_tasks(self) -> None:
        """Cancel SCID-owned background work during manager shutdown."""

        tasks = (
            list(self._observer_tasks)
            + list(self._candidate_tasks)
            + list(self._assessment_tasks)
            + list(self._speculative_watcher_tasks)
        )
        if self._pending_speculative_assessment is not None:
            tasks.append(self._pending_speculative_assessment)
        for task in tasks:
            if not task.done():
                task.cancel()

    def _cancel_active_assessments(self) -> None:
        state = self.blackboard.speculative_advance
        if state is not None:
            self._trace_for(state.source_interaction_seq).speculative_cancelled = True
            self.blackboard.complete_speculation()
        for task in list(self._assessment_tasks):
            if not task.done():
                task.cancel()

    async def start(self) -> str:
        """Render the first frontend turn."""

        self._started = True
        text = await self.dialogue_model.render(self.ledger.get_directive())
        self._save_partial_snapshot()
        return text

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

    def _promote_confirmed_speculative_reply(self) -> None:
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
        self.blackboard.complete_speculation()
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
            self.blackboard.request_repair(
                RepairRequest(
                    source_field_id=state.source_field_id,
                    speculative_field_id=state.speculative_field_id,
                    reason=self.ledger.pending_clarification,
                    required_slot="criterion_evidence",
                    suggested_action="clarify",
                ).snapshot()
            )
        repair_payload = self.blackboard.repair_pending
        if repair_payload is not None:
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
            repair_payload["announced"] = True
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
            self.blackboard.complete_speculation()
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

    async def _watch_speculative_assessment(
        self,
        *,
        assessment_task: asyncio.Task[Any],
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

        state = self.blackboard.speculative_advance
        if state is None or state.source_interaction_seq != interaction_seq:
            return
        if self.interaction_mode in {"crisis", "completed"} or self._finished:
            self._trace_for(interaction_seq).speculative_cancelled = True
            self.blackboard.complete_speculation()
            self._save_partial_snapshot()
            return
        if (
            self.ledger.current_field_id == speculative_field_id
            and self.ledger.pending_clarification is None
        ):
            self.blackboard.confirm_speculation()
            logger.info(
                "SCID speculative advance confirmed - seq: %s, field: %s",
                interaction_seq,
                speculative_field_id,
            )
        else:
            self._trace_for(interaction_seq).speculative_cancelled = True
            self.blackboard.request_repair(
                RepairRequest(
                    source_field_id=source_field_id,
                    speculative_field_id=speculative_field_id,
                    reason=(
                        self.ledger.pending_clarification
                        or "后台未确认上一字段可以推进"
                    ),
                    required_slot="criterion_evidence",
                    suggested_action="clarify",
                ).snapshot()
            )
            logger.info(
                "SCID speculative advance requires repair - seq: %s, field: %s",
                interaction_seq,
                source_field_id,
            )
        self._save_partial_snapshot()

    def _start_observer_task(
        self,
        *,
        user_text: str,
        interaction_seq: int,
        input_kind: str,
        interaction_turn: SCIDInteractionTurn | None,
    ) -> asyncio.Task[TurnInterpretation] | None:
        if self.observer_mode == "off":
            return None
        observer_version = self.blackboard.next_observer_version()
        context = self._build_observer_context(
            user_text=user_text,
            interaction_seq=interaction_seq,
            observer_version=observer_version,
            input_kind=input_kind,
        )
        task = asyncio.create_task(
            self._run_observer(
                context=context,
                interaction_turn=interaction_turn,
            )
        )
        self._observer_tasks.add(task)
        task.add_done_callback(self._observer_tasks.discard)
        return task

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
        interpretation = await self.observer.observe(context=context)
        interpretation.interaction_seq = seq
        interpretation.observer_version = int(context["observer_version"])
        interpretation.based_on_state_version = int(context["state_version"])
        interpretation.field_id = context.get("current_field_id")
        interpretation.input_kind = str(context["input_kind"])
        current = self.blackboard.apply_observation(interpretation)
        if interaction_turn is not None:
            interaction_turn.observer_decision = interpretation.snapshot()
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
            and self.runtime_mode == "realtime"
            and self.observer_mode == "active"
            and self.realtime_observer_planning_enabled
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
        )
        self.blackboard.set_partial_plan(plan)
        self._trace_for(interpretation.interaction_seq).partial_plan_ready_at = ready_at
        self._save_partial_snapshot()
        if directive is None or not self.enable_candidate_pregeneration:
            return
        candidate_texts = await self._generate_candidates(
            interaction_seq=interpretation.interaction_seq,
            field_id=field.field_id,
            state_version=interpretation.based_on_state_version,
            directives={interpretation.recommended_action: directive},
        )
        if self.blackboard.latest_partial_plan is plan:
            plan.candidate_texts.update(candidate_texts)
            self._save_partial_snapshot()

    def _promote_partial_plan_for_final(
        self,
        *,
        user_text: str,
        interaction_seq: int,
    ) -> PartialObserverPlan | None:
        """Promote a matching partial plan for final-turn arbitration."""

        plan = self.blackboard.latest_partial_plan
        if plan is None or not self.realtime_observer_planning_enabled:
            return None
        reason = self._partial_plan_rejection_reason(
            plan=plan,
            final_text=user_text,
            interaction_seq=interaction_seq,
        )
        trace = self._trace_for(interaction_seq)
        if reason:
            trace.partial_plan_rejected_reason = reason
            self.blackboard.reject_partial_plan(reason)
            return None
        promoted_at = now_ts()
        promoted = self.blackboard.promote_partial_plan(promoted_at=promoted_at)
        trace.partial_plan_promoted_at = promoted_at
        return promoted

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
        if now_ts() - plan.ready_at > self.partial_plan_max_age_seconds:
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
        }

    def _start_candidate_task(
        self,
        *,
        interaction_seq: int,
        field_id: str | None,
        state_version: int,
    ) -> asyncio.Task[dict[str, str]] | None:
        if not self.enable_candidate_pregeneration or field_id is None:
            return None
        next_field_id = self.ledger.preview_next_scan_field_id(field_id)
        if next_field_id is None:
            return None
        next_field = self.template.get_field(next_field_id)
        directive = DialogueDirective(
            directive_type="candidate_question",
            field_id=next_field_id,
            question_text=next_field.question_text,
            instruction=(
                "把候选扫描题自然、简短、非诱导地说出来。"
                "不要表示上一题已经判定或完成。"
            ),
            allowed_actions=["ask"],
        )
        task = asyncio.create_task(
            self._generate_candidates(
                interaction_seq=interaction_seq,
                field_id=field_id,
                state_version=state_version,
                directives={"ask_next_field": directive},
            )
        )
        self._candidate_tasks.add(task)
        task.add_done_callback(self._candidate_tasks.discard)
        return task

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
            return await self.candidate_cache.pre_generate(
                field_id=field_id,
                state_version=state_version,
                directives=directives,
            )
        finally:
            trace.candidate_generation_finished_at = now_ts()
            self._save_partial_snapshot()

    def claim_interaction_seq(self) -> int:
        """Reserve and return the next user-interaction sequence number."""

        self._latest_interaction_seq += 1
        return self._latest_interaction_seq

    def is_latest_interaction(self, interaction_seq: int | None) -> bool:
        """Return whether a pending async task still belongs to the latest turn."""

        return (
            interaction_seq is None or interaction_seq == self._latest_interaction_seq
        )

    def wait_text_for(self, user_text: str) -> str | None:
        """Return a delayed neutral response while backend routing/reasoning runs."""

        if (
            not self.enable_wait_text
            or self.is_finished
            or not user_text.strip()
            or self._is_obvious_asr_noise(user_text)
        ):
            return None
        return "我听到了，稍等我想一下。"

    async def accept_text(
        self,
        user_text: str,
        *,
        interaction_seq: int | None = None,
    ) -> SCIDRuntimeResponse:
        """Accept one ASR-final user interaction."""

        if self._finished:
            return SCIDRuntimeResponse(
                wait_text=None,
                final_text="",
                interaction_seq=interaction_seq,
            )
        if not self._started:
            await self.start()

        seq = self._claim_or_update_interaction_seq(interaction_seq)
        text = user_text.strip()
        if self._is_obvious_asr_noise(text):
            self.blackboard.reject_partial_plan("asr_noise")
            self._trace_for(seq).partial_plan_rejected_reason = "asr_noise"
            self.blackboard.stable_partial_text = ""
            turn = self._record_interaction(seq, text, ignored_reason="asr_noise")
            self._save_partial_snapshot()
            logger.info(
                "SCID ignored ASR noise - episode: %s, seq: %s, text: %r",
                self.episode_id,
                seq,
                text,
            )
            return SCIDRuntimeResponse(
                wait_text=None,
                final_text="",
                interaction_seq=seq,
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
        else:
            route = await self._route_text(text, seq)

        if route.route in {"crisis", "stop_scid"}:
            self._cancel_active_assessments()
        elif route.route == "scid_answer" and route.should_score:
            self._promote_confirmed_speculative_reply()

        turn = self._record_interaction(seq, text, route_decision=route)
        self.blackboard.begin_interaction(seq, text)
        self._start_observer_task(
            user_text=text,
            interaction_seq=seq,
            input_kind=("partial" if route.route == "scid_partial" else "final"),
            interaction_turn=turn,
        )
        if not self.is_latest_interaction(seq):
            turn.stale = True
            self._save_partial_snapshot()
            return SCIDRuntimeResponse(
                wait_text=None,
                final_text="",
                stale=True,
                interaction_seq=seq,
            )

        response = await self._handle_route(turn=turn, route=route)
        self._save_partial_snapshot()
        return response

    async def accept_text_progressive(
        self,
        user_text: str,
        *,
        interaction_seq: int | None = None,
    ) -> SCIDProgressiveRuntimeResponse:
        """Accept one ASR-final interaction and return progressive response parts."""

        if self._finished:
            return SCIDProgressiveRuntimeResponse(
                interaction_seq=interaction_seq or self._latest_interaction_seq,
                initial_text="",
                followup_task=None,
                terminal=True,
            )
        if not self._started:
            await self.start()

        seq = self._claim_or_update_interaction_seq(interaction_seq)
        text = user_text.strip()
        if self._is_obvious_asr_noise(text):
            self.blackboard.reject_partial_plan("asr_noise")
            self.blackboard.stable_partial_text = ""
            turn = self._record_interaction(seq, text, ignored_reason="asr_noise")
            self._save_partial_snapshot()
            return SCIDProgressiveRuntimeResponse(
                interaction_seq=seq,
                initial_text="",
                followup_task=None,
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
            route = await self._route_text(text, seq)

        if route.route in {"crisis", "stop_scid"}:
            self._cancel_active_assessments()
        elif route.route == "scid_answer" and route.should_score:
            self._promote_confirmed_speculative_reply()

        turn = self._record_interaction(seq, text, route_decision=route)
        self.blackboard.begin_interaction(seq, text)
        observer_task = self._start_observer_task(
            user_text=text,
            interaction_seq=seq,
            input_kind=("partial" if route.route == "scid_partial" else "final"),
            interaction_turn=turn,
        )
        if not self.is_latest_interaction(seq):
            turn.stale = True
            self.mark_interaction_stale(seq)
            self._save_partial_snapshot()
            return SCIDProgressiveRuntimeResponse(
                interaction_seq=seq,
                initial_text="",
                followup_task=None,
                stale=True,
            )

        if route.route == "scid_answer" and route.should_score:
            self.interaction_mode = "scid"
            initial_text = await self._render_bridge_text(turn=turn, route=route)
            record = self._record_progressive_turn(
                interaction_seq=seq,
                initial_text=initial_text,
                route=route.route,
            )
            if self._should_defer_for_speculation():
                self.blackboard.defer_speculative_reply(
                    interaction_seq=seq,
                    user_text=self._scoring_text_for(route, turn.user_text),
                )
                followup_task = asyncio.create_task(
                    self._resolve_speculative_reply(
                        turn=turn,
                        route=route,
                        record=record,
                    )
                )
            else:
                candidate_task = self._start_candidate_task(
                    interaction_seq=seq,
                    field_id=self.ledger.current_field_id,
                    state_version=self.ledger.state_version,
                )
                assessment_task = asyncio.create_task(
                    self._score_scid_answer(
                        turn=turn,
                        route=route,
                        allow_speculative_commit=self.enable_optimistic_scan,
                    )
                )
                self._assessment_tasks.add(assessment_task)
                assessment_task.add_done_callback(self._assessment_tasks.discard)
                followup_task = asyncio.create_task(
                    self._progressive_followup_text(
                        turn=turn,
                        route=route,
                        record=record,
                        observer_task=observer_task,
                        candidate_task=candidate_task,
                        assessment_task=assessment_task,
                        source_field_id=self.ledger.current_field_id,
                        source_state_version=self.ledger.state_version,
                    )
                )
            self._save_partial_snapshot()
            return SCIDProgressiveRuntimeResponse(
                interaction_seq=seq,
                initial_text=initial_text,
                followup_task=followup_task,
            )

        response = await self._handle_route(turn=turn, route=route)
        self._record_progressive_turn(
            interaction_seq=seq,
            initial_text=response.final_text,
            followup_text="",
            followup_stale=response.stale,
            route=route.route,
        )
        self._save_partial_snapshot()
        return SCIDProgressiveRuntimeResponse(
            interaction_seq=seq,
            initial_text=response.final_text,
            followup_task=None,
            stale=response.stale,
            terminal=self._finished,
        )

    async def accept_text_realtime(
        self,
        user_text: str,
        *,
        interaction_seq: int,
    ) -> SCIDRealtimeRuntimeResponse:
        """Accept one ASR-final interaction and return realtime streams."""

        if self._finished:
            return SCIDRealtimeRuntimeResponse(
                interaction_seq=interaction_seq,
                initial_stream=None,
                action_stream_task=None,
                terminal=True,
            )
        if not self._started:
            await self.start()

        seq = self._claim_or_update_interaction_seq(interaction_seq)
        text = user_text.strip()
        if self._is_obvious_asr_noise(text):
            self.blackboard.reject_partial_plan("asr_noise")
            self._trace_for(seq).partial_plan_rejected_reason = "asr_noise"
            self.blackboard.stable_partial_text = ""
            turn = self._record_interaction(seq, text, ignored_reason="asr_noise")
            self._record_realtime_turn(
                interaction_seq=seq,
                route="asr_noise",
                initial_directive=None,
            )
            self._save_partial_snapshot()
            return SCIDRealtimeRuntimeResponse(
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

        if route.route in {"crisis", "stop_scid"}:
            self._cancel_active_assessments()
        elif route.route == "scid_answer" and route.should_score:
            self._promote_confirmed_speculative_reply()

        promoted_partial_plan: PartialObserverPlan | None = None
        if route.route == "scid_answer" and route.should_score:
            promoted_partial_plan = self._promote_partial_plan_for_final(
                user_text=text,
                interaction_seq=seq,
            )
        else:
            reason = f"route_{route.route}"
            self.blackboard.reject_partial_plan(reason)
            self._trace_for(seq).partial_plan_rejected_reason = reason
        self.blackboard.stable_partial_text = ""

        turn = self._record_interaction(seq, text, route_decision=route)
        self.blackboard.begin_interaction(seq, text)
        observer_task = self._start_observer_task(
            user_text=text,
            interaction_seq=seq,
            input_kind=("partial" if route.route == "scid_partial" else "final"),
            interaction_turn=turn,
        )
        if not self.is_latest_interaction(seq):
            turn.stale = True
            self.mark_interaction_stale(seq)
            self._save_partial_snapshot()
            return SCIDRealtimeRuntimeResponse(
                interaction_seq=seq,
                initial_stream=None,
                action_stream_task=None,
                stale=True,
            )

        if route.route != "scid_answer" or not route.should_score:
            response = await self._handle_route(turn=turn, route=route)
            record = self._record_realtime_turn(
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
            return SCIDRealtimeRuntimeResponse(
                interaction_seq=seq,
                initial_stream=stream if response.final_text else None,
                action_stream_task=None,
                stale=response.stale,
                terminal=self._finished,
            )

        self.interaction_mode = "scid"
        directive = self._build_realtime_initial_directive(turn=turn, route=route)
        record = self._record_realtime_turn(
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
        action_stream_task = asyncio.create_task(
            self._realtime_action_stream(
                turn=turn,
                route=route,
                record=record,
                observer_task=observer_task,
                promoted_partial_plan=promoted_partial_plan,
                initial_boundary_event=initial_boundary_event,
                initial_cancelled_event=initial_cancelled_event,
            )
        )
        self._save_partial_snapshot()
        return SCIDRealtimeRuntimeResponse(
            interaction_seq=seq,
            initial_stream=initial_stream,
            action_stream_task=action_stream_task,
        )

    async def _handle_route(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
    ) -> SCIDRuntimeResponse:
        seq = turn.interaction_seq
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
            self.blackboard.clear_foreground_probe()
            directive = self._resume_directive(route)
            final_text = await self.dialogue_model.render(directive)
            return self._final_response(turn, final_text)

        if route.route == "pause_scid":
            self.interaction_mode = "paused"
            self.pending_user_buffer = ""
            self.blackboard.clear_foreground_probe()
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
            self.blackboard.clear_foreground_probe()
            self.status = "stopped"
            directive = DialogueDirective(
                directive_type="complete",
                field_id=None,
                question_text=route.safe_frontend_content
                or "可以，我们先结束这次评估。谢谢你的配合。",
                instruction="结束本次评估，不要继续追问。",
                allowed_actions=["stop"],
            )
            final_text = await self.dialogue_model.render(directive)
            turn.assistant_text = final_text
            self.finish(status="stopped")
            return SCIDRuntimeResponse(
                wait_text=None,
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

    async def _render_bridge_text(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
    ) -> str:
        trace = self._trace_for(turn.interaction_seq)
        trace.frontend_initial_started_at = now_ts()
        text = route.normalized_user_text or turn.user_text
        fallback_text = self._bridge_fallback_text(
            text,
            interaction_seq=turn.interaction_seq,
        )
        directive_type = "bridge_ack"
        if len(text) >= 24:
            directive_type = "bridge_reflect"
        elif self._is_uncertain_reply(text):
            directive_type = "bridge_hold"
        current_question = ""
        if self.ledger.current_field_id:
            current_question = self.template.get_field(
                self.ledger.current_field_id
            ).question_text
        directive = DialogueDirective(
            directive_type=directive_type,
            field_id=None,
            question_text=(
                fallback_text
                if isinstance(self.dialogue_model, RuleBasedDialogueModel)
                else ""
            ),
            instruction=(
                "你是实时前台对话模型。请根据 progress_text 里的用户原话，"
                "生成一到三句自然、非机械的回应。先直接回应，再严格基于用户原话做克制承接；"
                "短答可以较短，较长叙述应足够支持三到六秒自然朗读。"
                "不要每轮都说同一句话。不要评分、诊断、说字段已完成、说进入下一题或承诺模块跳转。"
                "这一段不要提出新问题，也不要重问当前题。"
                f"当前后台仍在核对的问题是：{current_question}"
            ),
            progress_text=f"用户原话：{text}",
            allowed_actions=["acknowledge"],
        )
        try:
            if isinstance(self.dialogue_model, RuleBasedDialogueModel):
                return fallback_text
            if self.frontend_initial_timeout_seconds <= 0:
                return fallback_text
            rendered = await asyncio.wait_for(
                self.dialogue_model.render(directive),
                timeout=self.frontend_initial_timeout_seconds,
            )
            return rendered.strip() or fallback_text
        except asyncio.TimeoutError:
            logger.info(
                "SCID frontend initial render timed out - episode: %s, seq: %s, timeout: %.2fs",
                self.episode_id,
                turn.interaction_seq,
                self.frontend_initial_timeout_seconds,
            )
            return fallback_text
        finally:
            trace.frontend_initial_finished_at = now_ts()

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
        trace = self._trace_for(interaction_seq)
        if phase == "initial":
            trace.frontend_stream_started_at = now_ts()
            trace.frontend_initial_started_at = trace.frontend_stream_started_at
        else:
            trace.frontend_followup_started_at = now_ts()
        if text:
            if phase == "initial":
                trace.frontend_first_token_at = (
                    trace.frontend_first_token_at or now_ts()
                )
            else:
                trace.foreground_action_first_token_at = (
                    trace.foreground_action_first_token_at or now_ts()
                )
            yield text
        if phase == "initial":
            trace.frontend_initial_finished_at = now_ts()
        else:
            trace.frontend_followup_finished_at = now_ts()
        if record is not None and record_key:
            record[record_key] = text
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
                completed = True
        except asyncio.CancelledError:
            cancelled_event.set()
            raise
        finally:
            close = getattr(source, "aclose", None)
            if callable(close):
                with suppress(RuntimeError):
                    await close()
            record["initial_text"] = "".join(emitted)
            if completed and self.is_latest_interaction(interaction_seq):
                boundary_at = now_ts()
                self._trace_for(interaction_seq).initial_semantic_boundary_at = (
                    boundary_at
                )
                record["initial_semantic_boundary_at"] = boundary_at
            else:
                cancelled_event.set()
            boundary_event.set()
            self._save_partial_snapshot()

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
        try:
            stream = (
                self.dialogue_model.stream(directive, context=context)
                if self.frontend_streaming
                else self._stream_text(
                    await self.dialogue_model.render(directive),
                    interaction_seq=interaction_seq,
                    phase=phase,
                    record=None,
                )
            )
            async for chunk in stream:
                if not chunk:
                    continue
                if not emitted:
                    if phase == "initial":
                        trace.frontend_first_token_at = now_ts()
                    else:
                        trace.foreground_action_first_token_at = now_ts()
                emitted.append(chunk)
                yield chunk
        finally:
            if phase == "initial":
                trace.frontend_initial_finished_at = now_ts()
            else:
                trace.frontend_followup_finished_at = now_ts()
            if record is not None and record_key:
                record[record_key] = "".join(emitted)
            self._save_partial_snapshot()

    async def _progressive_followup_text(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
        record: dict[str, Any],
        observer_task: asyncio.Task[TurnInterpretation] | None,
        candidate_task: asyncio.Task[dict[str, str]] | None,
        assessment_task: asyncio.Task[str],
        source_field_id: str | None,
        source_state_version: int,
    ) -> str:
        try:
            text = ""
            if (
                not self.enable_optimistic_scan
                or observer_task is None
                or candidate_task is None
                or source_field_id is None
            ):
                text = await assessment_task
            else:
                done, _ = await asyncio.wait(
                    {assessment_task, observer_task, candidate_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if assessment_task in done:
                    text = await assessment_task
                else:
                    interpretation = await observer_task
                    if assessment_task.done():
                        text = await assessment_task
                    else:
                        candidates = await candidate_task
                        if assessment_task.done():
                            text = await assessment_task
                        else:
                            plan = self.latency_controller.plan(
                                field=self.template.get_field(source_field_id),
                                interpretation=interpretation,
                                optimistic_scan_enabled=self.enable_optimistic_scan,
                                speculative_depth=self.blackboard.speculative_depth,
                                repair_pending=(
                                    self.blackboard.repair_pending is not None
                                ),
                            )
                            next_field_id = self.ledger.preview_next_scan_field_id(
                                source_field_id
                            )
                            candidate = candidates.get("ask_next_field", "")
                            state_is_current = (
                                self.ledger.current_field_id == source_field_id
                                and self.ledger.state_version == source_state_version
                            )
                            if (
                                plan.allow_speculation
                                and next_field_id
                                and candidate
                                and state_is_current
                            ):
                                self.blackboard.begin_speculation(
                                    source_field_id=source_field_id,
                                    speculative_field_id=next_field_id,
                                    source_interaction_seq=turn.interaction_seq,
                                    based_on_state_version=source_state_version,
                                    question_text=candidate,
                                )
                                trace = self._trace_for(turn.interaction_seq)
                                trace.speculative_advance = True
                                trace.speculative_field_id = next_field_id
                                self._pending_speculative_assessment = assessment_task
                                watcher = asyncio.create_task(
                                    self._watch_speculative_assessment(
                                        assessment_task=assessment_task,
                                        source_field_id=source_field_id,
                                        speculative_field_id=next_field_id,
                                        interaction_seq=turn.interaction_seq,
                                    )
                                )
                                self._speculative_watcher_tasks.add(watcher)
                                watcher.add_done_callback(
                                    self._speculative_watcher_tasks.discard
                                )
                                text = candidate
                            else:
                                text = await assessment_task
        except Exception as exc:
            trace = self._trace_for(turn.interaction_seq)
            trace.error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "SCID progressive followup failed - episode: %s, seq: %s",
                self.episode_id,
                turn.interaction_seq,
            )
            text = "刚才后台流程出现了技术问题，我们先暂停一下，我会重新核对状态。"
        turn.assistant_text = f"{record.get('initial_text', '')}{text}"
        record["followup_text"] = text
        record["followup_stale"] = turn.stale
        self._save_partial_snapshot()
        return text

    async def _realtime_action_stream(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
        record: dict[str, Any],
        observer_task: asyncio.Task[TurnInterpretation] | None,
        promoted_partial_plan: PartialObserverPlan | None,
        initial_boundary_event: asyncio.Event,
        initial_cancelled_event: asyncio.Event,
    ) -> AsyncIterator[str] | None:
        seq = turn.interaction_seq
        source_field_id = self.ledger.current_field_id
        source_state_version = self.ledger.state_version
        trace = self._trace_for(seq)
        broker = ForegroundActionBroker(
            interaction_seq=seq,
            state_version=source_state_version,
            field_id=source_field_id,
            max_followups=1,
        )
        assessment_task: asyncio.Task[Any] | None = None
        submitter: asyncio.Task[None] | None = None
        observer_submitter: asyncio.Task[None] | None = None

        try:
            if self._should_defer_for_speculation():
                self.blackboard.defer_speculative_reply(
                    interaction_seq=seq,
                    user_text=self._scoring_text_for(route, turn.user_text),
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
                trace.speech_action_committed_at = selected_at
                self._record_foreground_action(deferred_action)
                self.blackboard.mark_spoken_action(deferred_action.snapshot())
                record["selected_action"] = deferred_action.snapshot()
                record["selection_reason"] = "deferred_speculation_resolved"
                return self._stream_foreground_action(
                    deferred_action,
                    record=record,
                    rendered_text=text,
                )

            assessment_task = asyncio.create_task(
                self._score_scid_answer(
                    turn=turn,
                    route=route,
                    allow_speculative_commit=True,
                    render_response=False,
                )
            )
            self._assessment_tasks.add(assessment_task)
            assessment_task.add_done_callback(self._assessment_tasks.discard)

            submitter = asyncio.create_task(
                self._submit_assessment_foreground_action(
                    broker=broker,
                    assessment_task=assessment_task,
                    turn=turn,
                    record=record,
                )
            )
            if observer_task is not None and self.observer_mode == "active":
                observer_submitter = asyncio.create_task(
                    self._submit_observer_foreground_action(
                        broker=broker,
                        observer_task=observer_task,
                        interaction_seq=seq,
                        source_field_id=source_field_id,
                        source_state_version=source_state_version,
                    )
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
                await self._cancel_realtime_workers(
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
                if not assessment_task.done():
                    assessment_task.cancel()
                with suppress(asyncio.CancelledError):
                    await assessment_task
                if not submitter.done():
                    submitter.cancel()
                with suppress(asyncio.CancelledError):
                    await submitter
                if observer_submitter is not None:
                    if not observer_submitter.done():
                        observer_submitter.cancel()
                    with suppress(asyncio.CancelledError):
                        await observer_submitter
                turn.scid_turn_id = None
                trace.stale = False
                action = self._build_timeout_foreground_action(
                    interaction_seq=seq,
                    source_field_id=source_field_id,
                    source_state_version=source_state_version,
                    source_text=self._scoring_text_for(route, turn.user_text),
                )
                trace.foreground_action_ready_at = now_ts()
                trace.foreground_action_source = action.source
                trace.foreground_action_kind = action.kind
                await broker.submit(action)
                action = await broker.commit_best(timeout=0)
                if action is None:
                    return None

            rendered_action_text: str | None = None
            if (
                action.source in {"observer", "observer_partial"}
                and not action.speculative
            ):
                if assessment_task.done():
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
                            repair_required=(
                                record.get("repair_required", False)
                                or trace.repair_required
                            ),
                        ),
                        source="assessor",
                        priority=90,
                        directive=assessment_directive,
                    )
                else:
                    assessment_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await assessment_task
                    turn.scid_turn_id = None
                    submitter.cancel()
                    with suppress(asyncio.CancelledError):
                        await submitter
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
                    self.blackboard.request_foreground_probe(
                        {
                            "interaction_seq": seq,
                            "field_id": source_field_id,
                            "state_version": source_state_version,
                            "observer_action": action.directive.progress_text,
                            "question_text": action.directive.question_text,
                            "source_user_text": source_text,
                            "source": action.source,
                            "evidence_slot": action.evidence_slot,
                        }
                    )
                    self.blackboard.assessor_status = "deferred_for_observer_probe"
                    trace.stale = False

            if observer_submitter is not None and not observer_submitter.done():
                observer_submitter.cancel()
                with suppress(asyncio.CancelledError):
                    await observer_submitter

            broker_snapshot = broker.snapshot()
            selected_at = now_ts()
            trace.foreground_action_selected_at = selected_at
            trace.speech_action_committed_at = selected_at
            trace.foreground_action_source = action.source
            trace.foreground_action_kind = action.kind
            trace.foreground_action_superseded_count = len(
                broker_snapshot["superseded"]
            )
            self._record_foreground_action(action)
            self.blackboard.mark_spoken_action(action.snapshot())
            record["selected_action"] = action.snapshot()
            record["selection_reason"] = "highest_priority_at_initial_boundary"
            record["broker"] = broker_snapshot
            if (
                action.speculative
                and source_field_id is not None
                and action.directive.field_id is not None
                and self.ledger.current_field_id == source_field_id
                and self.ledger.state_version == source_state_version
                and self.blackboard.speculative_depth == 0
            ):
                self.blackboard.begin_speculation(
                    source_field_id=source_field_id,
                    speculative_field_id=action.directive.field_id,
                    source_interaction_seq=seq,
                    based_on_state_version=source_state_version,
                    question_text=action.directive.question_text,
                )
                trace.speculative_advance = True
                trace.speculative_field_id = action.directive.field_id
                self._pending_speculative_assessment = assessment_task
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
        except asyncio.CancelledError:
            trace.realtime_cancelled = True
            with suppress(asyncio.CancelledError):
                await self._cancel_realtime_workers(
                    assessment_task,
                    submitter,
                    observer_submitter,
                )
            self._save_partial_snapshot()
            raise

    @staticmethod
    async def _cancel_realtime_workers(
        *tasks: asyncio.Task[Any] | None,
    ) -> None:
        active = [task for task in tasks if task is not None and not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

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
            optimistic_scan_enabled=self.enable_optimistic_scan,
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
        if self.enable_candidate_pregeneration and source_field_id is not None:
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

    def _build_timeout_foreground_action(
        self,
        *,
        interaction_seq: int,
        source_field_id: str | None,
        source_state_version: int,
        source_text: str,
    ) -> ForegroundAction:
        """Keep the interview moving when no backend action meets its deadline."""

        state_unchanged = (
            self.ledger.current_field_id == source_field_id
            and self.ledger.state_version == source_state_version
        )
        if state_unchanged:
            question_text = (
                "我还在核对你刚才说的情况。为了不让对话停在这里，"
                "你能结合刚才的回答，再举一个最典型的例子吗？"
            )
            self.pending_user_buffer = self._combine_pending(
                self.pending_user_buffer,
                source_text,
            )
            self.blackboard.request_foreground_probe(
                {
                    "interaction_seq": interaction_seq,
                    "field_id": source_field_id,
                    "state_version": source_state_version,
                    "observer_action": "timeout_safe_probe",
                    "question_text": question_text,
                    "source_user_text": source_text,
                }
            )
            self.blackboard.assessor_status = "deferred_for_timeout_probe"
            directive = DialogueDirective(
                directive_type="observer_probe",
                field_id=source_field_id,
                question_text=question_text,
                instruction=(
                    "只问这个同字段安全追问，不要宣布字段完成或透露后台超时。"
                ),
                progress_text="timeout_safe_probe",
                allowed_actions=["clarify"],
            )
            kind = "clarify"
            source = "timeout_fallback"
        else:
            directive = self.ledger.get_directive()
            kind = "ask_committed"
            source = "timeout_recovery"

        return ForegroundAction(
            interaction_seq=interaction_seq,
            based_on_state_version=source_state_version,
            field_id=source_field_id,
            kind=kind,
            source=source,
            priority=20,
            directive=directive,
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
        async with self._ledger_lock:
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
            ledger_turn = self.ledger.begin_turn(scorer_text)
            turn.scid_turn_id = ledger_turn.turn_id
            self.blackboard.assessor_status = "running"
            self.blackboard.assessor_field_id = source_field_id
            assessment_owner = asyncio.current_task()
            if assessment_owner is not None:
                self._assessment_tasks.add(assessment_owner)

            try:
                trace.assessor_started_at = now_ts()
                decision = await self.assessor.assess(
                    ledger=self.ledger,
                    user_text=scorer_text,
                    turn_id=ledger_turn.turn_id,
                    observer_context=self._assessor_observer_context(source_field_id),
                )
            except asyncio.CancelledError:
                self.ledger.discard_turn_if_uncommitted(ledger_turn.turn_id)
                self.blackboard.assessor_status = "cancelled"
                trace.stale = True
                raise
            except Exception as exc:
                trace.repair_required = True
                logger.exception(
                    "SCID backend assess failed - episode: %s, turn: %s, field: %s",
                    self.episode_id,
                    ledger_turn.turn_id,
                    self.ledger.current_field_id,
                )
                decision = fallback_reask_decision(
                    field_id=self.ledger.current_field_id or "",
                    reason=f"后台模型调用失败：{type(exc).__name__}: {exc}",
                    question=(
                        "我刚才核对后台流程时遇到了一点技术问题。"
                        "我们先继续确认：你能再具体说说这种感觉出现时的情况吗？"
                    ),
                )
            finally:
                if assessment_owner is not None:
                    self._assessment_tasks.discard(assessment_owner)
                trace.assessor_finished_at = now_ts()
                if self.blackboard.assessor_status != "cancelled":
                    self.blackboard.assessor_status = "ready"

            trace.assessor_action = decision.next_action
            if trace.observer_action is not None:
                trace.observer_assessor_agree = self._observer_assessor_agree(
                    trace.observer_action,
                    decision.next_action,
                )

            if not self._assessment_result_is_applicable(
                interaction_seq=seq,
                source_field_id=source_field_id,
                source_state_version=source_state_version,
                allow_speculative_commit=allow_speculative_commit,
            ):
                turn.stale = True
                trace.stale = True
                self.ledger.discard_turn_if_uncommitted(ledger_turn.turn_id)
                self.blackboard.assessor_status = "stale"
                return ""

            try:
                self.ledger.apply_decision(
                    decision,
                    turn_id=ledger_turn.turn_id,
                    raw_user_text=scorer_text,
                    expected_field_id=source_field_id,
                    expected_state_version=source_state_version,
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
                    self.ledger.discard_turn_if_uncommitted(ledger_turn.turn_id)
                    self.blackboard.assessor_status = "stale"
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
                    self.ledger.apply_decision(
                        fallback,
                        turn_id=ledger_turn.turn_id,
                        raw_user_text=scorer_text,
                        expected_field_id=source_field_id,
                        expected_state_version=source_state_version,
                    )
                    trace.ledger_committed_at = now_ts()
                except LedgerValidationError:
                    logger.exception(
                        "SCID fallback decision also failed - episode: %s, turn: %s",
                        self.episode_id,
                        ledger_turn.turn_id,
                    )
                    self.ledger.discard_turn_if_uncommitted(ledger_turn.turn_id)
                    self.blackboard.assessor_status = "failed"
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

            self.blackboard.assessor_status = "committed"
            self.blackboard.sync_committed_state(
                state_version=self.ledger.state_version,
                current_field_id=self.ledger.current_field_id,
            )
            self.candidate_cache.invalidate_before(self.ledger.state_version)
            if (
                self.blackboard.repair_pending is not None
                and self.ledger.current_field_id != source_field_id
            ):
                self.blackboard.clear_repair()
            self.pending_user_buffer = ""
            self.blackboard.clear_foreground_probe()
            directive = self.ledger.get_directive()

        if self.ledger.terminal_status:
            self.interaction_mode = (
                "crisis" if self.ledger.terminal_status == "crisis" else "completed"
            )
            self.status = self.ledger.terminal_status
            self.finish(status=self.status)
        if not render_response:
            return directive

        trace.frontend_followup_started_at = now_ts()
        final_text = await self.dialogue_model.render(directive)
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
        if self.is_latest_interaction(interaction_seq):
            return True
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
        self.interaction_mode = "crisis"
        self.pending_user_buffer = ""
        self.blackboard.clear_foreground_probe()
        async with self._ledger_lock:
            if self.ledger.terminal_status != "crisis":
                ledger_turn = self.ledger.begin_turn(
                    route.normalized_user_text or turn.user_text
                )
                turn.scid_turn_id = ledger_turn.turn_id
                decision = fallback_reask_decision(
                    field_id=self.ledger.current_field_id or "",
                    reason=route.reasoning_summary or "Safety route interrupted SCID.",
                )
                decision.next_action = "crisis"
                decision.evidence = [turn.user_text]
                self.ledger.apply_decision(
                    decision,
                    turn_id=ledger_turn.turn_id,
                    raw_user_text=turn.user_text,
                )
                self.blackboard.sync_committed_state(
                    state_version=self.ledger.state_version,
                    current_field_id=self.ledger.current_field_id,
                )
                self.candidate_cache.clear()
            directive = self.ledger.get_directive()

        final_text = await self.dialogue_model.render(directive)
        self.status = "crisis"
        self.finish(status="crisis")
        return final_text

    async def _route_text(
        self, user_text: str, interaction_seq: int
    ) -> SCIDRouteDecision:
        context = self._build_router_context(user_text, interaction_seq)
        trace = self._trace_for(interaction_seq)
        trace.pre_router_started_at = now_ts()
        try:
            route = await self.router.route(context=context)
        finally:
            trace.pre_router_finished_at = now_ts()
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
        try:
            route = await self.control_router.route(context=context)
        finally:
            trace.pre_router_finished_at = now_ts()
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
        if text.startswith(pending) or pending in text:
            return text
        return f"{pending}{text}"

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
        return turn

    def _final_response(
        self,
        turn: SCIDInteractionTurn,
        final_text: str,
    ) -> SCIDRuntimeResponse:
        if turn.stale:
            final_text = ""
        turn.assistant_text = final_text
        return SCIDRuntimeResponse(
            wait_text=None,
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
        if trace.field_id is None:
            trace.field_id = self.ledger.current_field_id
        return trace

    def _record_progressive_turn(
        self,
        *,
        interaction_seq: int,
        initial_text: str,
        route: str,
        followup_text: str = "",
        followup_stale: bool = False,
    ) -> dict[str, Any]:
        record = {
            "interaction_seq": interaction_seq,
            "route": route,
            "initial_text": initial_text,
            "followup_text": followup_text,
            "followup_stale": followup_stale,
        }
        self.progressive_turns.append(record)
        return record

    def _record_realtime_turn(
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
            "repair_required": False,
        }
        self.realtime_turns.append(record)
        return record

    def _record_foreground_action(self, action: ForegroundAction) -> None:
        self.foreground_actions.append(action.snapshot())
        self.foreground_actions = self.foreground_actions[-100:]

    def _claim_or_update_interaction_seq(self, interaction_seq: int | None) -> int:
        if interaction_seq is None:
            return self.claim_interaction_seq()
        if interaction_seq > self._latest_interaction_seq:
            self._latest_interaction_seq = interaction_seq
        return interaction_seq

    @staticmethod
    def _is_obvious_asr_noise(text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return True
        if len(stripped) <= 3 and re.fullmatch(r"[A-Za-z]+", stripped):
            return True
        return False

    def finish(self, *, status: str | None = None) -> Path:
        """Save the current episode. Idempotent."""

        if self._episode_path is not None:
            self._finished = True
            return self._episode_path
        self.status = status or self.status
        self.ended_at = utc_now_iso()
        self.episode_dir.mkdir(parents=True, exist_ok=True)
        payload = self.snapshot()
        payload["ended_at"] = self.ended_at
        path = self.episode_dir / f"{self.episode_id}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._episode_path = path
        self._finished = True
        return path

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-friendly runtime snapshot."""

        return {
            "episode_id": self.episode_id,
            "user_id": self.user_id,
            "task": "scid_voice_assessment_v1",
            "experiment_id": self.experiment_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "runtime_mode": self.runtime_mode,
            "backend_model": self.backend_model,
            "observer_model": self.observer_model,
            "observer_mode": self.observer_mode,
            "candidate_pregeneration_enabled": self.enable_candidate_pregeneration,
            "optimistic_scan_enabled": self.enable_optimistic_scan,
            "frontend_streaming_enabled": self.frontend_streaming,
            "fast_policy_enabled": self.fast_policy.enabled,
            "realtime_observer_planning_enabled": (
                self.realtime_observer_planning_enabled
            ),
            "partial_plan_max_age_seconds": self.partial_plan_max_age_seconds,
            "post_initial_action_wait_seconds": (self.post_initial_action_wait_seconds),
            "interaction_mode": self.interaction_mode,
            "pending_user_buffer": self.pending_user_buffer,
            "active_task_state": {
                "latest_interaction_seq": self._latest_interaction_seq,
                "ledger_lock_locked": self._ledger_lock.locked(),
                "observer_task_count": len(self._observer_tasks),
                "candidate_task_count": len(self._candidate_tasks),
                "assessment_task_count": len(self._assessment_tasks),
                "speculative_watcher_count": len(self._speculative_watcher_tasks),
            },
            "route_decisions": [
                turn.route_decision
                for turn in self.interaction_turns
                if turn.route_decision is not None
            ],
            "interaction_turns": [turn.snapshot() for turn in self.interaction_turns],
            "progressive_turns": list(self.progressive_turns),
            "realtime_turns": list(self.realtime_turns),
            "foreground_actions": list(self.foreground_actions),
            "clinical_blackboard": self.blackboard.snapshot(),
            "candidate_utterance_cache": self.candidate_cache.snapshot(),
            "latency_traces": [
                trace.snapshot() for _, trace in sorted(self.latency_traces.items())
            ],
            "ledger": self.ledger.snapshot(),
        }

    def current_directive(self) -> DialogueDirective:
        """Return the current directive for tests/tools."""

        return self.ledger.get_directive()

    @property
    def partial_episode_path(self) -> Path:
        """Return the path used for the live, non-terminal debug snapshot."""

        return self.episode_dir / f"{self.episode_id}.partial.json"

    def _save_partial_snapshot(self) -> None:
        """Persist the current in-progress state for frontend smoke tests."""

        try:
            self.episode_dir.mkdir(parents=True, exist_ok=True)
            self.partial_episode_path.write_text(
                json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(
                "Failed to save SCID partial snapshot - episode: %s, error: %s",
                self.episode_id,
                exc,
            )
