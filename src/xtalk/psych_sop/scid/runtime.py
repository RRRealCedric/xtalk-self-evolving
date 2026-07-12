"""Orchestrator runtime for the SCID dual-LM voice assessment."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...log_utils import logger
from ..episode_logger import DEFAULT_EPISODE_DIR
from ..safety_guard import SafetyGuard
from .backend import BackgroundAssessor, create_background_assessor
from .decision import fallback_reask_decision
from .frontend import DialogueModel, RuleBasedDialogueModel
from .ledger import AssessmentLedger, LedgerValidationError
from .router import SCIDInteractionRouter, create_scid_interaction_router
from .schema import (
    DialogueDirective,
    SCIDInteractionTurn,
    SCIDRouteDecision,
    utc_now_iso,
)
from .template import load_scid_template


@dataclass(slots=True)
class SCIDRuntimeResponse:
    """Texts emitted for one accepted user interaction."""

    wait_text: str | None
    final_text: str
    stale: bool = False
    interaction_seq: int | None = None


class SCIDDualLMRuntime:
    """Coordinate frontend dialogue, routing, background assessment, and state."""

    def __init__(
        self,
        *,
        experiment_id: str = "scid_voice_demo",
        user_id: str = "xtalk_scid_demo_user",
        episode_dir: str | Path = DEFAULT_EPISODE_DIR,
        backend_model: str = "deepseek-v4-pro",
        prefer_deepseek: bool = True,
        assessor: BackgroundAssessor | None = None,
        router: SCIDInteractionRouter | None = None,
        dialogue_model: DialogueModel | None = None,
        enable_wait_text: bool = True,
    ) -> None:
        self.experiment_id = experiment_id
        self.user_id = user_id
        self.episode_dir = Path(episode_dir)
        self.template = load_scid_template()
        self.ledger = AssessmentLedger(template=self.template)
        self.assessor = assessor or create_background_assessor(
            model=backend_model,
            prefer_deepseek=prefer_deepseek,
        )
        self.router = router or create_scid_interaction_router(
            model=backend_model,
            prefer_deepseek=prefer_deepseek,
        )
        self.dialogue_model = dialogue_model or RuleBasedDialogueModel()
        self.safety_guard = SafetyGuard()
        self.enable_wait_text = enable_wait_text

        self.episode_id = str(uuid4())
        self.started_at = utc_now_iso()
        self.ended_at: str | None = None
        self.status = "in_progress"
        self.interaction_mode = "scid"
        self.pending_user_buffer = ""
        self.interaction_turns: list[SCIDInteractionTurn] = []

        self._started = False
        self._finished = False
        self._episode_path: Path | None = None
        self._latest_interaction_seq = 0
        self._ledger_lock = asyncio.Lock()

    @property
    def is_finished(self) -> bool:
        """Return whether the runtime has saved a terminal episode."""

        return self._finished

    @property
    def episode_path(self) -> Path | None:
        """Return the saved episode path, if any."""

        return self._episode_path

    async def start(self) -> str:
        """Render the first frontend turn."""

        self._started = True
        text = await self.dialogue_model.render(self.ledger.get_directive())
        self._save_partial_snapshot()
        return text

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

        turn = self._record_interaction(seq, text, route_decision=route)
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
            directive = self._resume_directive(route)
            final_text = await self.dialogue_model.render(directive)
            return self._final_response(turn, final_text)

        if route.route == "pause_scid":
            self.interaction_mode = "paused"
            self.pending_user_buffer = ""
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

    async def _score_scid_answer(
        self,
        *,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
    ) -> str:
        seq = turn.interaction_seq
        scorer_text = self._scoring_text_for(route, turn.user_text)
        async with self._ledger_lock:
            if not self.is_latest_interaction(seq):
                turn.stale = True
                return ""
            ledger_turn = self.ledger.begin_turn(scorer_text)
            turn.scid_turn_id = ledger_turn.turn_id

            try:
                decision = await self.assessor.assess(
                    ledger=self.ledger,
                    user_text=scorer_text,
                    turn_id=ledger_turn.turn_id,
                )
            except Exception as exc:
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

            if not self.is_latest_interaction(seq):
                turn.stale = True
                self.ledger.discard_turn_if_uncommitted(ledger_turn.turn_id)
                return ""

            try:
                self.ledger.apply_decision(
                    decision,
                    turn_id=ledger_turn.turn_id,
                    raw_user_text=scorer_text,
                )
            except LedgerValidationError as exc:
                if not self.is_latest_interaction(seq):
                    turn.stale = True
                    self.ledger.discard_turn_if_uncommitted(ledger_turn.turn_id)
                    return ""
                fallback = fallback_reask_decision(
                    field_id=self.ledger.current_field_id or "",
                    reason=f"后台决策未通过校验：{exc}",
                )
                try:
                    self.ledger.apply_decision(
                        fallback,
                        turn_id=ledger_turn.turn_id,
                        raw_user_text=scorer_text,
                    )
                except LedgerValidationError:
                    logger.exception(
                        "SCID fallback decision also failed - episode: %s, turn: %s",
                        self.episode_id,
                        ledger_turn.turn_id,
                    )
                    self.ledger.discard_turn_if_uncommitted(ledger_turn.turn_id)
                    return "我刚才核对流程时遇到了一点技术问题。我们先暂停一下。"

            self.pending_user_buffer = ""
            directive = self.ledger.get_directive()

        final_text = await self.dialogue_model.render(directive)
        if not self.is_latest_interaction(seq):
            turn.stale = True
            return ""

        if self.ledger.terminal_status:
            self.interaction_mode = (
                "crisis" if self.ledger.terminal_status == "crisis" else "completed"
            )
            self.status = self.ledger.terminal_status
            self.finish(status=self.status)
        return final_text

    async def _apply_crisis(
        self,
        turn: SCIDInteractionTurn,
        route: SCIDRouteDecision,
    ) -> str:
        self.interaction_mode = "crisis"
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
            directive = self.ledger.get_directive()

        final_text = await self.dialogue_model.render(directive)
        self.status = "crisis"
        self.finish(status="crisis")
        return final_text

    async def _route_text(
        self, user_text: str, interaction_seq: int
    ) -> SCIDRouteDecision:
        context = self._build_router_context(user_text, interaction_seq)
        return await self.router.route(context=context)

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
            "interaction_mode": self.interaction_mode,
            "pending_user_buffer": self.pending_user_buffer,
            "active_task_state": {
                "latest_interaction_seq": self._latest_interaction_seq,
                "ledger_lock_locked": self._ledger_lock.locked(),
            },
            "route_decisions": [
                turn.route_decision
                for turn in self.interaction_turns
                if turn.route_decision is not None
            ],
            "interaction_turns": [turn.snapshot() for turn in self.interaction_turns],
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
