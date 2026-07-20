"""Natural repair directives after an optimistic SCID step is rejected."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..core.schema import DialogueDirective, utc_now_iso


@dataclass(slots=True)
class RepairRequest:
    """Request to revisit an invalidated optimistic SCID step.

    Parameters
    ----------
    source_field_id : str
        Field that requires renewed clarification.
    speculative_field_id : str | None
        Speculative field reached before the repair was requested.
    reason : str
        Internal reason for invalidating the optimistic step.
    required_slot : str
        Evidence slot that must be clarified.
    suggested_action : str
        Recommended repair action.
    severity : str, optional
        Repair severity used by orchestration policy.
    announced : bool, optional
        Whether the repair has already been presented to the user.
    created_at : str, optional
        UTC ISO timestamp. Generated automatically when omitted.
    """

    source_field_id: str
    speculative_field_id: str | None
    reason: str
    required_slot: str
    suggested_action: str
    severity: str = "normal"
    announced: bool = False
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = utc_now_iso()

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable representation of the repair request.

        Returns
        -------
        dict[str, Any]
            Repair-request fields keyed by attribute name.
        """

        return asdict(self)


def build_repair_directive(
    request: RepairRequest,
    *,
    clarification_question: str,
) -> DialogueDirective:
    """Build a safe user-facing directive for a repair request.

    Parameters
    ----------
    request : RepairRequest
        Repair metadata identifying the field to revisit.
    clarification_question : str
        Preferred clarification wording. A generic prompt is used when empty.

    Returns
    -------
    DialogueDirective
        Directive that revisits the source field without exposing internals.
    """

    question = clarification_question.strip() or (
        "我回到刚才那个问题再确认一下。你能结合自己的经历再具体说说吗？"
    )
    return DialogueDirective(
        directive_type="repair_prompt",
        field_id=request.source_field_id,
        question_text=f"我回到刚才那一点再确认一下。{question}",
        instruction=(
            "自然说明需要回到上一点确认，然后只问 question_text。"
            "不要说模型出错、判分失败或暴露字段编号。"
        ),
        allowed_actions=["clarify_wording", "respect_stop"],
    )
