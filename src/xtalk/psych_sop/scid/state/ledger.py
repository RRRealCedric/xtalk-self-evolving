"""Deterministic ledger for SCID field state and traversal."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..core.schema import (
    AssessmentDecision,
    DialogueDirective,
    SCIDField,
    SCIDFieldState,
    SCIDTemplate,
    SCIDTurn,
    VALID_ACTIONS,
    VALID_SCORES,
)
from ..core.template import module_prefix


class LedgerValidationError(ValueError):
    """Raised when a background-LM decision cannot be safely applied."""


@dataclass
class AssessmentLedger:
    """Authoritative SCID runtime state.

    The manager/orchestrator owns this ledger. LLMs may propose decisions, but
    only this class can commit scores, evidence, and traversal.
    """

    template: SCIDTemplate
    current_field_id: str | None = None
    field_states: dict[str, SCIDFieldState] = field(default_factory=dict)
    turns: list[SCIDTurn] = field(default_factory=list)
    queued_module_fields: list[str] = field(default_factory=list)
    completed_scan: bool = False
    terminal_status: str | None = None
    pending_clarification: str | None = None
    state_version: int = 0
    archived_turn_count: int = 0
    _turn_id_counter: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._turn_id_counter = max(
            (turn.turn_id for turn in self.turns),
            default=0,
        )
        if self.current_field_id is None:
            self.current_field_id = self.template.first_field_id()

    @property
    def current_field(self) -> SCIDField | None:
        """Return the current field, if the interview is still active."""

        if self.current_field_id is None:
            return None
        return self.template.get_field(self.current_field_id)

    def next_turn_id(self) -> int:
        """Return the id that will be assigned to the next user turn."""

        return self._turn_id_counter + 1

    def begin_turn(self, user_text: str, *, interaction_seq: int) -> SCIDTurn:
        """Create a new turn for the current field."""

        if self.terminal_status is not None or self.current_field_id is None:
            raise LedgerValidationError(
                "Cannot begin a turn after the ledger is terminal"
            )
        if type(interaction_seq) is not int or interaction_seq < 1:
            raise LedgerValidationError("interaction_seq must be a positive integer")
        if not isinstance(user_text, str) or not user_text.strip():
            raise LedgerValidationError("Turn user_text must be a non-empty string")
        if len(user_text) > 8192:
            raise LedgerValidationError("Turn user_text exceeds 8192 characters")
        if any(turn.decision is None for turn in self.turns):
            raise LedgerValidationError(
                "Cannot begin a turn while another turn is uncommitted"
            )
        if any(turn.interaction_seq == interaction_seq for turn in self.turns):
            raise LedgerValidationError("interaction_seq has already created a turn")
        if self.turns and interaction_seq <= self.turns[-1].interaction_seq:
            raise LedgerValidationError("interaction_seq is stale or out of order")
        turn = SCIDTurn(
            turn_id=self.next_turn_id(),
            interaction_seq=interaction_seq,
            field_id=self.current_field_id,
            user_text=user_text,
        )
        self.turns.append(turn)
        self._turn_id_counter = turn.turn_id
        return turn

    def discard_turn_if_uncommitted(self, turn_id: int) -> bool:
        """Remove the latest turn if no decision has been committed to it."""

        if not self.turns:
            return False
        turn = self.turns[-1]
        if turn.turn_id != turn_id or turn.decision is not None:
            return False
        self.turns.pop()
        return True

    def get_directive(self) -> DialogueDirective:
        """Return the next frontend-safe dialogue directive."""

        if self.terminal_status == "crisis":
            return DialogueDirective(
                directive_type="crisis",
                field_id=None,
                question_text="",
                instruction=(
                    "停止评估流程，用简短、明确、支持性的语言提醒用户优先保证安全，"
                    "联系当地紧急服务、可信任的人或专业机构。"
                ),
                safety_note="Do not continue SCID questions.",
            )
        if self.terminal_status == "completed":
            return DialogueDirective(
                directive_type="complete",
                field_id=None,
                question_text="",
                instruction="告诉用户本阶段 SCID 访谈已经完成，并感谢配合。",
            )

        field = self.current_field
        if field is None:
            return DialogueDirective(
                directive_type="complete",
                field_id=None,
                question_text="",
                instruction="告诉用户本阶段 SCID 访谈已经完成，并感谢配合。",
            )

        if self.pending_clarification:
            return DialogueDirective(
                directive_type="clarify",
                field_id=field.field_id,
                question_text=self.pending_clarification,
                instruction=("只转述这次澄清问题。不要替用户判断答案，不要说明分数。"),
                progress_text=self._progress_text(),
            )

        return DialogueDirective(
            directive_type="ask",
            field_id=field.field_id,
            question_text=field.question_text,
            instruction=(
                "用自然、简短、非诱导的口语问出当前问题。"
                "可以稍微缓和语气，但不要改变题意，不要提分数。"
            ),
            progress_text=self._progress_text(),
        )

    def build_assessor_context(self, turn: SCIDTurn) -> dict[str, Any]:
        """Build the background-LM context payload for one turn."""

        field = self.current_field
        recent_states = [
            state.snapshot() for state in list(self.field_states.values())[-12:]
        ]
        recent_turns = [item.snapshot() for item in self.turns[-8:]]
        return {
            "turn_id": turn.turn_id,
            "interaction_seq": turn.interaction_seq,
            "state_version": self.state_version,
            "current_node": field.snapshot() if field else None,
            "current_field_id": self.current_field_id,
            "user_text": turn.user_text,
            "pending_clarification": self.pending_clarification,
            "filled_state": recent_states,
            "recent_turns": recent_turns,
            "queued_module_fields": list(self.queued_module_fields),
            "scoring_rules": {
                "?": "资料不足",
                "1": "无或否",
                "2": "阈下",
                "3": "阈上或是",
            },
        }

    def apply_decision(
        self,
        decision: AssessmentDecision,
        *,
        turn_id: int,
        expected_field_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> None:
        """Validate and apply one background-LM decision."""

        if type(turn_id) is not int or turn_id < 1:
            raise LedgerValidationError("turn_id must be a positive integer")
        if expected_state_version is not None and (
            type(expected_state_version) is not int or expected_state_version < 0
        ):
            raise LedgerValidationError(
                "expected_state_version must be a non-negative integer"
            )
        if (
            expected_state_version is not None
            and expected_state_version != self.state_version
        ):
            raise LedgerValidationError(
                "Decision is based on a stale ledger state version"
            )
        if expected_field_id is not None and expected_field_id != self.current_field_id:
            raise LedgerValidationError("Decision is based on a stale ledger field")
        self._validate_decision(decision, turn_id=turn_id)
        turn = self._turn_by_id(turn_id)
        turn.decision = decision.snapshot()

        if decision.next_action == "crisis":
            self.terminal_status = "crisis"
            self.current_field_id = None
            self.pending_clarification = None
            self.state_version += 1
            return

        if decision.next_action in {"clarify", "reask"}:
            self.pending_clarification = (
                decision.clarification_question.strip()
                or "我还需要多确认一点。你能结合刚才这个问题再具体说说吗？"
            )
            self.state_version += 1
            return

        if decision.score is None:
            raise LedgerValidationError(
                "Advance/branch decision unexpectedly has no score"
            )
        if self.current_field_id is None:
            raise LedgerValidationError(
                "Advance/branch decision has no active ledger field"
            )
        self.field_states[self.current_field_id] = SCIDFieldState(
            field_id=self.current_field_id,
            score=decision.score,
            confidence=decision.confidence,
            evidence=decision.evidence,
            raw_user_text=turn.user_text,
            reasoning_summary=decision.reasoning_summary,
            turn_id=turn_id,
        )
        self.pending_clarification = None

        field = self.template.get_field(self.current_field_id)
        if field.kind == "scan":
            self._maybe_queue_priority_module(field=field, score=decision.score)
        self._advance_current_field()
        self.state_version += 1

    def preview_next_scan_field_id(self, field_id: str | None = None) -> str | None:
        """Return the next scan field without mutating committed state."""

        current = field_id or self.current_field_id
        if current not in self.template.scan_order:
            return None
        index = self.template.scan_order.index(current) + 1
        if index >= len(self.template.scan_order):
            return None
        return self.template.scan_order[index]

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-friendly ledger snapshot."""

        return {
            "template": self.template.snapshot(),
            "state_version": self.state_version,
            "current_field_id": self.current_field_id,
            "completed_scan": self.completed_scan,
            "terminal_status": self.terminal_status,
            "pending_clarification": self.pending_clarification,
            "queued_module_fields": list(self.queued_module_fields),
            "archived_turn_count": self.archived_turn_count,
            "field_states": {
                field_id: state.snapshot()
                for field_id, state in self.field_states.items()
            },
            "turns": [turn.snapshot() for turn in self.turns],
        }

    def archive_committed_turns(self, *, max_recent: int = 16) -> int:
        """Keep only a bounded recent window after events have been projected."""

        if type(max_recent) is not int or max_recent < 1:
            raise ValueError("max_recent must be positive")
        if len(self.turns) <= max_recent:
            return 0
        archive_count = len(self.turns) - max_recent
        candidates = self.turns[:archive_count]
        if any(turn.decision is None for turn in candidates):
            raise LedgerValidationError("Cannot archive an uncommitted ledger turn")
        self.turns = self.turns[archive_count:]
        self.archived_turn_count += archive_count
        return archive_count

    def _validate_decision(
        self,
        decision: AssessmentDecision,
        *,
        turn_id: int,
    ) -> None:
        if self.terminal_status is not None or self.current_field_id is None:
            raise LedgerValidationError("Cannot apply a decision to a terminal ledger")
        if not self.turns or self.turns[-1].turn_id != turn_id:
            raise LedgerValidationError("Decision turn_id is stale or unknown")
        turn = self._turn_by_id(turn_id)
        if turn.decision is not None:
            raise LedgerValidationError(
                "Decision has already been committed for this turn"
            )
        if turn.field_id != self.current_field_id:
            raise LedgerValidationError("Turn is based on a stale ledger field")
        if not isinstance(decision.next_action, str) or (
            decision.next_action not in VALID_ACTIONS
        ):
            raise LedgerValidationError(f"Invalid action: {decision.next_action}")
        if (
            not isinstance(decision.field_id, str)
            or not decision.field_id.strip()
            or len(decision.field_id) > 256
        ):
            raise LedgerValidationError(
                "Decision field_id must be a non-empty bounded string"
            )
        if (
            decision.next_action != "crisis"
            and decision.field_id != self.current_field_id
        ):
            raise LedgerValidationError(
                f"Decision field_id {decision.field_id!r} does not match current "
                f"field {self.current_field_id!r}"
            )
        if isinstance(decision.confidence, bool) or not isinstance(
            decision.confidence, (int, float)
        ):
            raise LedgerValidationError("Decision confidence must be a number")
        try:
            confidence = float(decision.confidence)
        except (OverflowError, ValueError) as exc:
            raise LedgerValidationError(
                "Decision confidence must be finite and within [0, 1]"
            ) from exc
        if not math.isfinite(confidence) or not (0.0 <= confidence <= 1.0):
            raise LedgerValidationError(
                "Decision confidence must be finite and within [0, 1]"
            )
        if not isinstance(decision.evidence, list):
            raise LedgerValidationError("Decision evidence must be a list")
        if len(decision.evidence) > 32:
            raise LedgerValidationError(
                "Decision evidence may contain at most 32 items"
            )
        if any(
            not isinstance(item, str) or not item.strip() or len(item.strip()) > 4096
            for item in decision.evidence
        ):
            raise LedgerValidationError(
                "Decision evidence must contain only non-empty bounded strings"
            )
        if decision.score is not None and (
            not isinstance(decision.score, str) or decision.score not in VALID_SCORES
        ):
            raise LedgerValidationError(f"Invalid score: {decision.score!r}")
        if (
            not isinstance(decision.clarification_question, str)
            or len(decision.clarification_question) > 4096
        ):
            raise LedgerValidationError(
                "Decision clarification_question must be a bounded string"
            )
        if (
            not isinstance(decision.reasoning_summary, str)
            or len(decision.reasoning_summary) > 8192
        ):
            raise LedgerValidationError(
                "Decision reasoning_summary must be a bounded string"
            )
        if decision.next_action in {"advance", "branch"}:
            if decision.score not in {"1", "2", "3"}:
                raise LedgerValidationError(
                    "Advance/branch decisions require a committed score"
                )
            if not decision.evidence:
                raise LedgerValidationError("Advance/branch decisions require evidence")
        if decision.next_action in {"clarify", "reask"}:
            if not isinstance(decision.clarification_question, str) or not (
                decision.clarification_question.strip()
            ):
                raise LedgerValidationError("Clarify/reask requires a question")

    def _turn_by_id(self, turn_id: int) -> SCIDTurn:
        for turn in self.turns:
            if turn.turn_id == turn_id:
                return turn
        raise LedgerValidationError(f"Unknown turn_id: {turn_id}")

    def _maybe_queue_priority_module(self, *, field: SCIDField, score: str) -> None:
        target = field.target_field_id
        prefix = module_prefix(target)
        if score != "3" or prefix not in self.template.priority_modules:
            return
        if (
            target
            and target in self.template.fields
            and target not in self.field_states
        ):
            if target not in self.queued_module_fields:
                self.queued_module_fields.append(target)

    def _advance_current_field(self) -> None:
        if self.current_field_id in self.template.scan_order:
            index = self.template.scan_order.index(self.current_field_id)
            next_index = index + 1
            if next_index < len(self.template.scan_order):
                self.current_field_id = self.template.scan_order[next_index]
                return
            self.completed_scan = True

        while self.queued_module_fields:
            next_field = self.queued_module_fields.pop(0)
            if next_field not in self.field_states:
                self.current_field_id = next_field
                return

        self.current_field_id = None
        self.terminal_status = "completed"

    def _progress_text(self) -> str:
        return ""
