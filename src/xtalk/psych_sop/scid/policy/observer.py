"""Asynchronous multi-label observer for all SCID conversation turns."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any

from langchain.chat_models.base import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ....log_utils import logger
from ..assessment.decision import DecisionParseError, extract_json_object_text
from ..core.schema import TurnInterpretation, VALID_OBSERVER_ACTIONS


class ObserverParseError(ValueError):
    """Raised when observer output cannot be validated."""


class IncrementalObserver(ABC):
    """Interpret every turn without mutating the assessment ledger."""

    @abstractmethod
    async def observe(self, *, context: dict[str, Any]) -> TurnInterpretation:
        """Return a provisional, multi-label interpretation."""


def parse_turn_interpretation(text: str) -> TurnInterpretation:
    """Parse model text into a validated turn interpretation.

    Parameters
    ----------
    text : str
        Model response containing a JSON object.

    Returns
    -------
    TurnInterpretation
        Validated provisional interpretation of the turn.

    Raises
    ------
    ObserverParseError
        If the response is not a JSON object or its payload is invalid.
    """

    try:
        payload = json.loads(extract_json_object_text(text))
    except (json.JSONDecodeError, DecisionParseError) as exc:
        raise ObserverParseError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise ObserverParseError("Observer payload must be a JSON object")
    return turn_interpretation_from_payload(payload)


def turn_interpretation_from_payload(
    payload: dict[str, Any],
) -> TurnInterpretation:
    """Build a turn interpretation from a JSON-like payload.

    Parameters
    ----------
    payload : dict[str, Any]
        Observer fields to validate and convert.

    Returns
    -------
    TurnInterpretation
        Typed provisional interpretation populated from ``payload``.

    Raises
    ------
    ObserverParseError
        If required fields are missing, the action is invalid, or the payload
        attempts to make a forbidden clinical commitment.
    """

    forbidden = {"score", "diagnosis", "field_completed"} & payload.keys()
    if forbidden:
        raise ObserverParseError(
            f"Observer payload contains forbidden clinical commitments: {sorted(forbidden)}"
        )
    required = {
        "interaction_seq",
        "observer_version",
        "based_on_state_version",
        "field_id",
        "dialogue_acts",
        "current_field_relevance",
        "related_field_ids",
        "related_module_ids",
        "contextual_memories",
        "evidence_candidates",
        "recommended_action",
        "missing_slots",
        "needs_deep_assessment",
        "commit_required",
        "confidence",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise ObserverParseError(f"Observer payload missing keys: {missing}")

    action = str(payload.get("recommended_action") or "").strip()
    if action not in VALID_OBSERVER_ACTIONS:
        raise ObserverParseError(f"Invalid observer action: {action!r}")

    return TurnInterpretation(
        interaction_seq=int(payload["interaction_seq"]),
        observer_version=int(payload["observer_version"]),
        based_on_state_version=int(payload["based_on_state_version"]),
        field_id=(str(payload["field_id"]).strip() if payload["field_id"] else None),
        dialogue_acts=_string_list(payload["dialogue_acts"]),
        current_field_relevance=_confidence(payload["current_field_relevance"]),
        related_field_ids=_string_list(payload["related_field_ids"]),
        related_module_ids=_string_list(payload["related_module_ids"]),
        contextual_memories=_dict_list(payload["contextual_memories"]),
        evidence_candidates=_dict_list(payload["evidence_candidates"]),
        recommended_action=action,  # type: ignore[arg-type]
        missing_slots=_string_list(payload["missing_slots"]),
        needs_deep_assessment=bool(payload["needs_deep_assessment"]),
        commit_required=bool(payload["commit_required"]),
        confidence=_confidence(payload["confidence"]),
        input_kind=str(payload.get("input_kind") or "final"),
        source=str(payload.get("source") or "llm"),
        raw_payload=dict(payload),
    )


class RuleBasedObserver(IncrementalObserver):
    """Fast deterministic observer for tests and no-key deployments."""

    async def observe(self, *, context: dict[str, Any]) -> TurnInterpretation:
        """Interpret one turn with deterministic lexical rules.

        Parameters
        ----------
        context : dict[str, Any]
            Current field, interaction metadata, and user text.

        Returns
        -------
        TurnInterpretation
            Provisional labels, candidate evidence, and recommended action.
        """

        text = str(context.get("user_text") or "").strip()
        lowered = text.lower()
        field = context.get("current_field") or {}
        field_id = str(field.get("field_id") or "").strip() or None
        module = str(field.get("module") or "").strip()
        kind = str(context.get("input_kind") or "final")
        seq = int(context.get("interaction_seq") or 0)

        acts: list[str] = []
        if kind == "partial":
            acts.append("partial")
        if any(mark in text for mark in ("?", "？", "什么意思", "为什么", "多少")):
            acts.append("question")
        if _contains_any(
            text, ("小时候", "父母", "家庭", "搬家", "恋爱", "伴侣", "工作")
        ):
            acts.append("self_disclosure")
        if _contains_any(text, ("不是", "更正", "刚才说错", "其实")):
            acts.append("correction")

        explicit_answer = _contains_any(
            lowered,
            (
                "有",
                "没有",
                "没",
                "是",
                "不是",
                "对",
                "经常",
                "偶尔",
                "不确定",
                "不知道",
            ),
        )
        clinical_detail = _contains_any(
            lowered,
            ("焦虑", "担心", "害怕", "紧张", "惊恐", "回避", "睡不着", "影响"),
        )
        if explicit_answer or clinical_detail:
            acts.append("answer")
        if not acts:
            acts.append("narrative")

        relevance = 0.9 if explicit_answer else 0.72 if clinical_detail else 0.28
        contextual_memories: list[dict[str, Any]] = []
        if "self_disclosure" in acts or relevance < 0.5:
            contextual_memories.append(
                {
                    "type": "contextual_history",
                    "content": text,
                    "source_interaction_seq": seq,
                    "status": "context_only",
                }
            )

        evidence_candidates: list[dict[str, Any]] = []
        if relevance >= 0.7 and field_id:
            evidence_candidates.append(
                {
                    "field_id": field_id,
                    "quote": text,
                    "slot": "direct_response",
                    "supports": "undetermined",
                    "source_interaction_seq": seq,
                    "status": "candidate",
                }
            )

        missing_slots: list[str] = []
        if kind == "partial" and _looks_incomplete_partial(text):
            action = "hold_for_assessor"
            confidence = 0.96
        elif _contains_any(lowered, ("自杀", "不想活", "伤害自己", "杀了别人")):
            action = "request_safety_review"
            confidence = 0.98
        elif _contains_any(lowered, ("不知道", "不确定", "说不清")):
            action = "repeat_current_question"
            missing_slots = ["direct_response"]
            confidence = 0.82
        elif field.get("latency_mode") == "optimistic_scan" and _is_clear_scan_answer(
            text
        ):
            action = "ask_next_field"
            confidence = 0.94
        elif relevance < 0.5:
            action = "hold_for_assessor"
            confidence = 0.76
        elif not _contains_any(lowered, ("多久", "个月", "年", "周", "天")):
            action = "ask_duration"
            missing_slots = ["duration"]
            confidence = 0.74
        elif not _contains_any(lowered, ("每天", "经常", "偶尔", "有时", "频率")):
            action = "ask_frequency"
            missing_slots = ["frequency"]
            confidence = 0.72
        else:
            action = "hold_for_assessor"
            confidence = 0.68

        return TurnInterpretation(
            interaction_seq=seq,
            observer_version=int(context.get("observer_version") or 0),
            based_on_state_version=int(context.get("state_version") or 0),
            field_id=field_id,
            dialogue_acts=list(dict.fromkeys(acts)),
            current_field_relevance=relevance,
            related_field_ids=[field_id] if field_id and relevance >= 0.5 else [],
            related_module_ids=[module] if module and relevance >= 0.5 else [],
            contextual_memories=contextual_memories,
            evidence_candidates=evidence_candidates,
            recommended_action=action,  # type: ignore[arg-type]
            missing_slots=missing_slots,
            needs_deep_assessment=(kind == "final" and relevance >= 0.5),
            commit_required=action in {"request_safety_review"},
            confidence=confidence,
            input_kind=kind,
            source="rule",
        )


class DeepSeekObserver(IncrementalObserver):
    """Low-latency DeepSeek observer used asynchronously or in shadow mode."""

    SYSTEM_PROMPT = """你是 SCID 对话的后台增量观察器，不是判分器，也不是前台助手。
你会看到一个有限的当前字段上下文和一轮用户原话。对每轮话语做多标签理解，提取候选证据和背景记忆，并建议下一类动作。
所有输出都只是 candidate，不能给 ?/1/2/3 分数，不能宣布字段完成，也不能直接修改流程。
背景经历、性格描述和看似题外的叙述可以进入 contextual_memories，但不能伪装成 criterion evidence。
evidence_candidates 必须保留用户原话 quote、关联 field_id、slot、source_interaction_seq 和 status=candidate。
当 recommended_action 只是围绕当前字段询问 duration、frequency、most_of_day、impairment、time_window 或重复当前问题时，commit_required 应为 false；只有动作必须等待正式判分或安全复核时才设为 true。
当 input_kind=partial 时，输入已经经过稳定窗口过滤。若文本已经形成完整语义，仍应给出可供 final 后复用的候选动作；若明显没有说完，则使用 hold_for_assessor。
选择 ask_duration、ask_frequency、ask_most_of_day、ask_impairment 或 clarify_time_window 时，missing_slots 必须包含对应的 duration、frequency、most_of_day、impairment 或 time_window。

recommended_action 只能是：ask_next_field、ask_duration、ask_frequency、ask_most_of_day、ask_impairment、clarify_time_window、repeat_current_question、hold_for_assessor、request_safety_review。

只输出以下 JSON 对象：
{
  "interaction_seq": 1,
  "observer_version": 1,
  "based_on_state_version": 0,
  "field_id": "S1-F3",
  "dialogue_acts": ["answer", "self_disclosure"],
  "current_field_relevance": 0.85,
  "related_field_ids": ["S1-F3"],
  "related_module_ids": ["F"],
  "contextual_memories": [],
  "evidence_candidates": [],
  "recommended_action": "ask_next_field",
  "missing_slots": [],
  "needs_deep_assessment": true,
  "commit_required": false,
  "confidence": 0.91,
  "input_kind": "final",
  "source": "llm"
}
"""

    def __init__(
        self,
        *,
        model: str = "deepseek-v4-flash",
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        chat_model: BaseChatModel | None = None,
    ) -> None:
        self.model_name = model
        self.fallback = RuleBasedObserver()
        self.chat_model = chat_model or ChatOpenAI(
            model=model,
            api_key=api_key or os.getenv("DEEPSEEK_API_KEY"),
            base_url=base_url,
            temperature=0.0,
            max_tokens=600,
            model_kwargs={"response_format": {"type": "json_object"}},
            extra_body={"thinking": {"type": "disabled"}},
        )

    async def observe(self, *, context: dict[str, Any]) -> TurnInterpretation:
        """Interpret one turn with DeepSeek and a deterministic fallback.

        Parameters
        ----------
        context : dict[str, Any]
            Current field, interaction metadata, and user text.

        Returns
        -------
        TurnInterpretation
            Parsed model interpretation, or a rule-based interpretation when
            model invocation or validation fails.
        """

        logger.info(
            "SCID observer start - model: %s, seq: %s, version: %s, kind: %s",
            self.model_name,
            context.get("interaction_seq"),
            context.get("observer_version"),
            context.get("input_kind"),
        )
        try:
            response = await self.chat_model.ainvoke(
                [
                    SystemMessage(content=self.SYSTEM_PROMPT),
                    HumanMessage(
                        content=json.dumps(context, ensure_ascii=False, indent=2)
                    ),
                ]
            )
            interpretation = parse_turn_interpretation(str(response.content or ""))
            logger.info(
                "SCID observer ready - seq: %s, action: %s, confidence: %.2f",
                interpretation.interaction_seq,
                interpretation.recommended_action,
                interpretation.confidence,
            )
            return interpretation
        except Exception as exc:
            logger.warning(
                "SCID observer failed; using rule fallback - seq: %s, error: %s",
                context.get("interaction_seq"),
                exc,
            )
            return await self.fallback.observe(context=context)


def create_incremental_observer(
    *,
    model: str = "deepseek-v4-flash",
    prefer_deepseek: bool = True,
    api_key: str | None = None,
    base_url: str = "https://api.deepseek.com",
) -> IncrementalObserver:
    """Create an incremental observer for the available configuration.

    Parameters
    ----------
    model : str, optional
        DeepSeek model name used when remote observation is enabled.
    prefer_deepseek : bool, optional
        Whether to select DeepSeek when an API key is available.
    api_key : str | None, optional
        Explicit DeepSeek API key. When omitted, ``DEEPSEEK_API_KEY`` is read
        from the environment.
    base_url : str, optional
        Base URL for the DeepSeek-compatible chat API.

    Returns
    -------
    IncrementalObserver
        DeepSeek-backed observer when enabled and configured; otherwise the
        deterministic rule-based observer.
    """

    resolved_api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
    if prefer_deepseek and resolved_api_key:
        return DeepSeekObserver(
            model=model,
            api_key=resolved_api_key,
            base_url=base_url,
        )
    return RuleBasedObserver()


def _contains_any(text: str, values: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(value.lower() in lowered for value in values)


def _is_clear_scan_answer(text: str) -> bool:
    stripped = text.strip().lower()
    if stripped in {
        "有",
        "有过",
        "有这种感觉",
        "没有",
        "没有过",
        "没有这种感觉",
        "没",
        "对",
        "是",
        "不是",
        "从来没有",
        "不确定",
        "不知道",
    }:
        return True
    if len(stripped) > 18:
        return False
    return _contains_any(
        stripped, ("有一点", "偶尔", "有时", "经常")
    ) and _contains_any(
        stripped,
        ("感觉", "担心", "害怕", "焦虑", "紧张", "惊恐", "回避"),
    )


def _looks_incomplete_partial(text: str) -> bool:
    stripped = text.strip(" ，,。.!！?？")
    if len(stripped) < 2:
        return True
    return stripped.endswith(
        (
            "比如",
            "比如说",
            "因为",
            "然后",
            "就是",
            "所以",
            "我想",
            "我觉得",
            "可能是",
        )
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ObserverParseError("Expected a JSON array of strings")
    return [str(item).strip() for item in value if str(item).strip()]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ObserverParseError("Expected a JSON array of objects")
    if any(not isinstance(item, dict) for item in value):
        raise ObserverParseError("Expected a JSON array of objects")
    return [dict(item) for item in value]


def _confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ObserverParseError(f"Invalid confidence: {value!r}") from exc
    return max(0.0, min(1.0, parsed))
