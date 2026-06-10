"""Rule-based SOP navigation for the psychology demo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .sop_schema import NextAction, SOPNode, SOPSpec


DEFAULT_SOP_PATH = Path(__file__).with_name("sop_template.yaml")


class SOPNavigator:
    """Navigate the editable psychology SOP with simple rules.

    TODO(psychology): refine SOP with PsychologySOP-Template.
    TODO(evolution): implement human-in-the-loop SOP patch proposal.
    """

    def __init__(self, sop_spec: SOPSpec) -> None:
        self.sop_spec = sop_spec
        self.current_node_id = "START"

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> "SOPNavigator":
        spec_path = Path(path) if path else DEFAULT_SOP_PATH
        data = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        nodes = {
            item["id"]: SOPNode(
                id=item["id"],
                goal=item.get("goal", ""),
                allowed_actions=list(item.get("allowed_actions", [])),
                transitions=list(item.get("transitions", [])),
                prompt_hints=list(item.get("prompt_hints", [])),
            )
            for item in data.get("nodes", [])
        }
        return cls(
            SOPSpec(
                sop_id=str(data.get("sop_id", "psych_scale_interview_v0.1")),
                global_rules=list(data.get("global_rules", [])),
                crisis_response=str(data.get("crisis_response", "")),
                nodes=nodes,
            )
        )

    def current_node(self) -> SOPNode:
        return self.sop_spec.nodes[self.current_node_id]

    def allowed_actions(self) -> list[str]:
        return list(self.current_node().allowed_actions)

    def set_node(self, node_id: str) -> None:
        if node_id not in self.sop_spec.nodes:
            raise KeyError(node_id)
        self.current_node_id = node_id

    def step(
        self, user_input: str, context: dict[str, Any] | None = None
    ) -> NextAction:
        """Advance the SOP using deterministic rules."""

        context = context or {}
        text = user_input.strip()
        lowered = text.lower()
        node_id = self.current_node_id

        if self._is_quit(lowered):
            return self._move("ABORTED", "abort", "user_quits", should_end=True)

        if context.get("safety_interrupt"):
            return self._move(
                "CRISIS_RESPONSE",
                "crisis_response",
                "safety_interrupt",
                should_end=True,
            )

        if node_id == "START":
            return self._move("EXPLAIN_BOUNDARY", "explain_boundary", "always")
        if node_id == "EXPLAIN_BOUNDARY":
            return self._move("GET_CONSENT", "ask_consent", "always")
        if node_id == "GET_CONSENT":
            if self._is_accept(lowered):
                return self._move("GOAL_INQUIRY", "ask_goal", "user_accepts")
            if self._is_refuse(lowered):
                return self._move("ABORTED", "abort", "user_refuses", should_end=True)
            return NextAction(node_id, "ask_consent", "needs_clear_consent")
        if node_id == "GOAL_INQUIRY":
            return self._move("SCALE_SELECTION", "select_scale", "user_responds")
        if node_id == "SCALE_SELECTION":
            selected_scale = context.get("selected_scale")
            if selected_scale:
                return self._move(
                    "RISK_CHECK",
                    "risk_check",
                    "scale_selected",
                    data={"scale_id": selected_scale},
                )
            return NextAction(node_id, "select_scale", "needs_scale_selection")
        if node_id == "RISK_CHECK":
            if context.get("high_risk"):
                return self._move(
                    "CRISIS_RESPONSE",
                    "crisis_response",
                    "high_risk",
                    should_end=True,
                )
            return self._move("SCALE_LOOP", "ask_scale_item", "no_high_risk")
        if node_id == "SCALE_LOOP":
            if context.get("needs_clarification"):
                return self._move("CLARIFY_ITEM", "clarify_item", "needs_clarification")
            if context.get("user_skips"):
                return self._move("RECORD_ANSWER", "skip_question", "user_skips")
            if context.get("answer_recorded"):
                return self._move("RECORD_ANSWER", "record_answer", "answer_recorded")
            return NextAction(node_id, "ask_scale_item", "awaiting_answer")
        if node_id == "CLARIFY_ITEM":
            return self._move("SCALE_LOOP", "ask_scale_item", "always")
        if node_id == "RECORD_ANSWER":
            if context.get("has_next_question"):
                return self._move("SCALE_LOOP", "ask_scale_item", "has_next_question")
            return self._move("COMPUTE_SCORE", "compute_score", "scale_completed")
        if node_id == "COMPUTE_SCORE":
            return self._move("EXPLAIN_RESULT", "explain_result", "score_computed")
        if node_id == "EXPLAIN_RESULT":
            return self._move(
                "SUPPORTIVE_CLOSE",
                "supportive_close",
                "always",
                should_end=True,
            )
        if node_id in {"SUPPORTIVE_CLOSE", "CRISIS_RESPONSE", "ABORTED"}:
            return NextAction(node_id, "end", "terminal", should_end=True)
        return NextAction(node_id, "noop", "unknown_node")

    def _move(
        self,
        node_id: str,
        action: str,
        reason: str,
        *,
        should_end: bool = False,
        data: dict[str, Any] | None = None,
    ) -> NextAction:
        self.set_node(node_id)
        return NextAction(node_id, action, reason, should_end, data or {})

    @staticmethod
    def _is_quit(text: str) -> bool:
        return text in {"退出", "停止", "结束", "不做了", "quit", "exit", "stop"}

    @staticmethod
    def _is_accept(text: str) -> bool:
        return text in {
            "是",
            "好",
            "好的",
            "可以",
            "同意",
            "继续",
            "yes",
            "y",
            "ok",
            "开始",
        }

    @staticmethod
    def _is_refuse(text: str) -> bool:
        return text in {"否", "不", "不要", "不同意", "no", "n"}
