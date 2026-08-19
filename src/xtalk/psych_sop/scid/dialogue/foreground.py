"""Foreground action arbitration for realtime SCID speech."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from ..core.schema import DialogueDirective, SCIDField
from ..orchestration.action_policy import (
    ActionBudget,
    ActionEligibilityPolicy,
    ActionRankingPolicy,
)


@dataclass(slots=True)
class ForegroundAction:
    """One safe user-facing action proposed by SCID runtime components."""

    interaction_seq: int
    based_on_state_version: int
    field_id: str | None
    kind: str
    source: str
    priority: int
    directive: DialogueDirective
    evidence_slot: str | None = None
    provisional: bool = False
    source_observer_version: int | None = None
    speculative: bool = False
    terminal: bool = False
    action_id: str = field(default_factory=lambda: str(uuid4()))

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""

        payload = asdict(self)
        payload["directive"] = self.directive.snapshot()
        return payload


class ForegroundActionBroker:
    """Collect foreground actions and select the best current one."""

    def __init__(
        self,
        *,
        interaction_seq: int,
        state_version: int,
        field_id: str | None,
        speculation_active: bool = False,
        eligibility_policy: ActionEligibilityPolicy | None = None,
        ranking_policy: ActionRankingPolicy | None = None,
        action_budget: ActionBudget | None = None,
    ) -> None:
        self.interaction_seq = interaction_seq
        self.state_version = state_version
        self.field_id = field_id
        self.speculation_active = speculation_active
        self.eligibility_policy = eligibility_policy or ActionEligibilityPolicy()
        self.ranking_policy = ranking_policy or ActionRankingPolicy()
        self.action_budget = action_budget or ActionBudget()
        self._condition = asyncio.Condition()
        self._actions: list[ForegroundAction] = []
        self._seen_keys: set[tuple[str, str]] = set()
        self._committed_action: ForegroundAction | None = None
        self._superseded: list[dict[str, Any]] = []
        self._rejected_stale: list[dict[str, Any]] = []
        self._closed = False

    async def submit(self, action: ForegroundAction) -> bool:
        """Submit one action candidate. Return False when it is stale."""

        async with self._condition:
            eligibility = self.eligibility_policy.evaluate(
                action,
                interaction_seq=self.interaction_seq,
                state_version=self.state_version,
                field_id=self.field_id,
                speculation_active=self.speculation_active,
            )
            if (
                self._closed
                or self._committed_action is not None
                or not eligibility.allowed
            ):
                rejected = action.snapshot()
                rejected["reason_code"] = (
                    "broker_closed"
                    if self._closed or self._committed_action is not None
                    else eligibility.reason_code
                )
                self._rejected_stale.append(rejected)
                return False
            key = (action.kind, action.directive.question_text.strip())
            if key in self._seen_keys:
                existing = next(
                    (
                        item
                        for item in self._actions
                        if (item.kind, item.directive.question_text.strip()) == key
                    ),
                    None,
                )
                if existing is None or action.priority <= existing.priority:
                    return False
                self._actions.remove(existing)
                self._superseded.append(
                    {
                        "action": existing.snapshot(),
                        "superseded_by": action.action_id,
                    }
                )
                self._actions.append(action)
                self._actions.sort(key=self.ranking_policy.rank, reverse=True)
                self._condition.notify_all()
                return True
            previous_best = self._actions[0] if self._actions else None
            self._seen_keys.add(key)
            self._actions.append(action)
            self._actions.sort(key=self.ranking_policy.rank, reverse=True)
            current_best = self._actions[0]
            if (
                previous_best is not None
                and current_best.action_id != previous_best.action_id
            ):
                self._superseded.append(
                    {
                        "action": previous_best.snapshot(),
                        "superseded_by": current_best.action_id,
                    }
                )
            self._condition.notify_all()
        return True

    async def commit_best(
        self,
        *,
        timeout: float | None = None,
    ) -> ForegroundAction | None:
        """Commit the best available action and lock foreground selection."""

        async with self._condition:
            if self._committed_action is not None:
                return None
            deadline = (
                None
                if timeout is None
                else asyncio.get_running_loop().time() + max(0.0, timeout)
            )
            while not self._actions and not self._closed:
                remaining = (
                    None
                    if deadline is None
                    else deadline - asyncio.get_running_loop().time()
                )
                if remaining is not None and remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(
                        self._condition.wait(),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    return None
            if self._closed or not self._actions:
                return None
            self._committed_action = self._actions[0]
            self._closed = True
            return self._committed_action

    @property
    def committed_action(self) -> ForegroundAction | None:
        """Return the action locked in for foreground delivery.

        Returns
        -------
        ForegroundAction | None
            Committed action, or ``None`` before selection.
        """

        return self._committed_action

    def snapshot(self) -> dict[str, Any]:
        """Return the broker state as a serializable mapping.

        Returns
        -------
        dict[str, Any]
            Current queue, commitment, supersession, and stale-rejection data.
        """

        return {
            "interaction_seq": self.interaction_seq,
            "state_version": self.state_version,
            "field_id": self.field_id,
            "queued": [action.snapshot() for action in self._actions],
            "committed": (
                self._committed_action.snapshot()
                if self._committed_action is not None
                else None
            ),
            "selected": (
                self._committed_action.snapshot()
                if self._committed_action is not None
                else None
            ),
            "superseded": list(self._superseded),
            "rejected_stale": list(self._rejected_stale),
            "closed": self._closed,
            "policy_version": self.ranking_policy.version,
            "action_budget": asdict(self.action_budget),
        }

    async def close(self) -> None:
        """Stop waiting for more actions."""

        async with self._condition:
            self._closed = True
            self._condition.notify_all()


class FastForegroundPolicy:
    """Local low-latency policy for obvious non-committal foreground actions."""

    def action_for_scan_answer(
        self,
        *,
        interaction_seq: int,
        state_version: int,
        field: SCIDField | None,
        next_field: SCIDField | None,
        speculative_depth: int,
        repair_pending: bool,
        user_text: str,
    ) -> ForegroundAction | None:
        """Return a safe candidate next question for a clear scan answer."""

        if (
            field is None
            or next_field is None
            or field.kind != "scan"
            or field.latency_mode != "optimistic_scan"
            or field.safety_sensitive
            or speculative_depth > 0
            or repair_pending
            or not _is_clear_short_answer(user_text)
        ):
            return None
        directive = DialogueDirective(
            directive_type="candidate_question",
            field_id=next_field.field_id,
            question_text=next_field.question_text,
            instruction=("自然问出候选下一题。不要说上一题已完成、已判定或已记分。"),
            allowed_actions=["ask"],
        )
        return ForegroundAction(
            interaction_seq=interaction_seq,
            based_on_state_version=state_version,
            field_id=field.field_id,
            kind="ask_candidate",
            source="fast_policy",
            priority=40,
            directive=directive,
            speculative=True,
        )


def _is_clear_short_answer(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 24:
        return False
    positive = ("有", "有过", "是", "对", "嗯", "会")
    negative = ("没有", "没", "不是", "不会", "完全没有", "完完全没有")
    uncertain = (
        "不知道",
        "不确定",
        "说不清",
        "应该",
        "好像",
        "可能",
        "大概",
        "也许",
        "似乎",
        "未必",
        "吧",
    )
    if any(item in stripped for item in uncertain):
        return False
    return any(item in stripped for item in positive + negative)
