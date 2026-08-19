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
from ..core.validation import (
    MAX_JSON_ARRAY_ITEMS,
    strict_bool,
    strict_finite_float,
    strict_json_loads,
    strict_nonnegative_int,
    strict_string,
    strict_string_list,
)


_OBSERVER_SLOT_BY_ACTION = {
    "ask_duration": "duration",
    "ask_frequency": "frequency",
    "ask_most_of_day": "most_of_day",
    "ask_impairment": "impairment",
    "clarify_time_window": "time_window",
    "repeat_current_question": "direct_response",
}
_VALID_EVIDENCE_SLOTS = {
    "direct_response",
    "duration",
    "frequency",
    "most_of_day",
    "impairment",
    "time_window",
}
_VALID_DIALOGUE_ACTS = {
    "answer",
    "correction",
    "narrative",
    "partial",
    "question",
    "self_disclosure",
}
_VALID_OBSERVER_SOURCES = {"llm", "rule", "test"}
_VALID_MEMORY_TYPES = {"contextual_history"}
_VALID_EVIDENCE_SUPPORT = {"contradicts", "supports", "undetermined"}


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
        payload = strict_json_loads(extract_json_object_text(text))
    except (ValueError, DecisionParseError) as exc:
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

    if not isinstance(payload, dict):
        raise ObserverParseError("Observer payload must be a JSON object")
    if any(not isinstance(key, str) for key in payload):
        raise ObserverParseError("Observer payload keys must be strings")
    forbidden = {"score", "diagnosis", "field_completed"} & payload.keys()
    if forbidden:
        raise ObserverParseError(
            "Observer payload contains forbidden clinical commitments: "
            f"{sorted(forbidden)}"
        )
    allowed = {
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
        "input_kind",
        "source",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ObserverParseError(f"Observer payload contains unknown keys: {unknown}")
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

    try:
        action = strict_string(
            payload["recommended_action"],
            field_name="recommended_action",
            maximum_length=64,
        )
    except ValueError as exc:
        raise ObserverParseError(str(exc)) from exc
    if action not in VALID_OBSERVER_ACTIONS:
        raise ObserverParseError(f"Invalid observer action: {action!r}")

    try:
        field_id = _optional_bounded_string(payload["field_id"], "field_id", 256)
        dialogue_acts = strict_string_list(
            payload["dialogue_acts"],
            field_name="dialogue_acts",
            maximum_item_length=128,
        )
        invalid_acts = sorted(set(dialogue_acts) - _VALID_DIALOGUE_ACTS)
        if invalid_acts:
            raise ValueError(f"Invalid dialogue_acts: {invalid_acts}")
        related_field_ids = strict_string_list(
            payload["related_field_ids"],
            field_name="related_field_ids",
            maximum_item_length=256,
        )
        related_module_ids = strict_string_list(
            payload["related_module_ids"],
            field_name="related_module_ids",
            maximum_item_length=64,
        )
        missing_slots = strict_string_list(
            payload["missing_slots"],
            field_name="missing_slots",
            maximum_item_length=64,
        )
        invalid_slots = sorted(set(missing_slots) - _VALID_EVIDENCE_SLOTS)
        if invalid_slots:
            raise ValueError(f"Invalid missing_slots: {invalid_slots}")
        required_slot = _OBSERVER_SLOT_BY_ACTION.get(action)
        if required_slot is not None and required_slot not in missing_slots:
            raise ValueError(
                f"recommended_action {action!r} requires missing slot {required_slot!r}"
            )

        commit_required = strict_bool(
            payload["commit_required"],
            field_name="commit_required",
        )
        if action in _OBSERVER_SLOT_BY_ACTION and commit_required:
            raise ValueError(
                f"recommended_action {action!r} cannot require a ledger commit"
            )
        input_kind = strict_string(
            payload.get("input_kind", "final"),
            field_name="input_kind",
            maximum_length=16,
        )
        if input_kind not in {"final", "partial"}:
            raise ValueError("input_kind must be 'final' or 'partial'")

        source = strict_string(
            payload.get("source", "llm"),
            field_name="source",
            maximum_length=64,
        )
        if source not in _VALID_OBSERVER_SOURCES:
            raise ValueError(f"Invalid observer source: {source!r}")

        return TurnInterpretation(
            interaction_seq=strict_nonnegative_int(
                payload["interaction_seq"], field_name="interaction_seq"
            ),
            observer_version=strict_nonnegative_int(
                payload["observer_version"], field_name="observer_version"
            ),
            based_on_state_version=strict_nonnegative_int(
                payload["based_on_state_version"],
                field_name="based_on_state_version",
            ),
            field_id=field_id,
            dialogue_acts=dialogue_acts,
            current_field_relevance=strict_finite_float(
                payload["current_field_relevance"],
                field_name="current_field_relevance",
                minimum=0.0,
                maximum=1.0,
            ),
            related_field_ids=related_field_ids,
            related_module_ids=related_module_ids,
            contextual_memories=_parse_contextual_memories(
                payload["contextual_memories"]
            ),
            evidence_candidates=_parse_evidence_candidates(
                payload["evidence_candidates"]
            ),
            recommended_action=action,  # type: ignore[arg-type]
            missing_slots=missing_slots,
            needs_deep_assessment=strict_bool(
                payload["needs_deep_assessment"],
                field_name="needs_deep_assessment",
            ),
            commit_required=commit_required,
            confidence=strict_finite_float(
                payload["confidence"],
                field_name="confidence",
                minimum=0.0,
                maximum=1.0,
            ),
            input_kind=input_kind,
            source=source,
            raw_payload=dict(payload),
        )
    except ValueError as exc:
        raise ObserverParseError(str(exc)) from exc


def validate_observer_provenance(
    interpretation: TurnInterpretation,
    *,
    interaction_seq: int,
    observer_version: int,
    state_version: int,
    field_id: str | None,
    module_id: str | None,
    user_text: str,
    input_kind: str,
) -> TurnInterpretation:
    """Fail closed unless observer claims match the immutable call context.

    This check must run after the asynchronous model call and before the result
    is written to the Blackboard or passed to the assessor.  It deliberately
    validates rather than overwrites provenance supplied by the model.
    """

    # Custom observer implementations can return a dataclass without using the
    # JSON parser.  Re-validate and canonicalize that object at this boundary.
    interpretation = turn_interpretation_from_payload(
        {
            "interaction_seq": interpretation.interaction_seq,
            "observer_version": interpretation.observer_version,
            "based_on_state_version": interpretation.based_on_state_version,
            "field_id": interpretation.field_id,
            "dialogue_acts": interpretation.dialogue_acts,
            "current_field_relevance": interpretation.current_field_relevance,
            "related_field_ids": interpretation.related_field_ids,
            "related_module_ids": interpretation.related_module_ids,
            "contextual_memories": interpretation.contextual_memories,
            "evidence_candidates": interpretation.evidence_candidates,
            "recommended_action": interpretation.recommended_action,
            "missing_slots": interpretation.missing_slots,
            "needs_deep_assessment": interpretation.needs_deep_assessment,
            "commit_required": interpretation.commit_required,
            "confidence": interpretation.confidence,
            "input_kind": interpretation.input_kind,
            "source": interpretation.source,
        }
    )
    expected = {
        "interaction_seq": (interpretation.interaction_seq, interaction_seq),
        "observer_version": (interpretation.observer_version, observer_version),
        "based_on_state_version": (
            interpretation.based_on_state_version,
            state_version,
        ),
        "field_id": (interpretation.field_id, field_id),
        "input_kind": (interpretation.input_kind, input_kind),
    }
    mismatched = [
        name for name, (actual, wanted) in expected.items() if actual != wanted
    ]
    if mismatched:
        raise ObserverParseError(
            f"Observer provenance mismatch: {', '.join(mismatched)}"
        )

    if any(related != field_id for related in interpretation.related_field_ids):
        raise ObserverParseError("related_field_ids contains an unrelated field")
    if any(related != module_id for related in interpretation.related_module_ids):
        raise ObserverParseError("related_module_ids contains an unrelated module")

    for index, memory in enumerate(interpretation.contextual_memories):
        if memory["source_interaction_seq"] != interaction_seq:
            raise ObserverParseError(
                f"contextual_memories[{index}] interaction sequence mismatch"
            )
        if memory["status"] != "context_only":
            raise ObserverParseError(
                f"contextual_memories[{index}] must have status=context_only"
            )
        if memory["content"] not in user_text:
            raise ObserverParseError(
                f"contextual_memories[{index}] content is not traceable to this turn"
            )

    for index, candidate in enumerate(interpretation.evidence_candidates):
        if candidate["source_interaction_seq"] != interaction_seq:
            raise ObserverParseError(
                f"evidence_candidates[{index}] interaction sequence mismatch"
            )
        if candidate["field_id"] != field_id:
            raise ObserverParseError(f"evidence_candidates[{index}] field mismatch")
        if candidate["status"] != "candidate":
            raise ObserverParseError(
                f"evidence_candidates[{index}] must have status=candidate"
            )
        if candidate["slot"] not in _VALID_EVIDENCE_SLOTS:
            raise ObserverParseError(
                f"evidence_candidates[{index}] has an invalid slot"
            )
        if candidate["quote"] not in user_text:
            raise ObserverParseError(
                f"evidence_candidates[{index}] quote is not traceable to this turn"
            )
    return interpretation


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


def _optional_bounded_string(
    value: Any,
    field_name: str,
    maximum_length: int,
) -> str | None:
    if value is None:
        return None
    return strict_string(
        value,
        field_name=field_name,
        maximum_length=maximum_length,
    )


def _parse_contextual_memories(value: Any) -> list[dict[str, Any]]:
    items = _object_list(value, field_name="contextual_memories")
    parsed: list[dict[str, Any]] = []
    required = {"content", "source_interaction_seq", "status"}
    allowed = required | {"type"}
    for index, item in enumerate(items):
        _validate_object_keys(
            item,
            field_name=f"contextual_memories[{index}]",
            required=required,
            allowed=allowed,
        )
        memory = {
            "content": strict_string(
                item["content"],
                field_name=f"contextual_memories[{index}].content",
                maximum_length=8192,
            ),
            "source_interaction_seq": strict_nonnegative_int(
                item["source_interaction_seq"],
                field_name=f"contextual_memories[{index}].source_interaction_seq",
            ),
            "status": strict_string(
                item["status"],
                field_name=f"contextual_memories[{index}].status",
                maximum_length=32,
            ),
        }
        if "type" in item:
            memory_type = strict_string(
                item["type"],
                field_name=f"contextual_memories[{index}].type",
                maximum_length=64,
            )
            if memory_type not in _VALID_MEMORY_TYPES:
                raise ValueError(f"contextual_memories[{index}].type is invalid")
            memory["type"] = memory_type
        parsed.append(memory)
    return parsed


def _parse_evidence_candidates(value: Any) -> list[dict[str, Any]]:
    items = _object_list(
        value,
        field_name="evidence_candidates",
        maximum_items=32,
    )
    parsed: list[dict[str, Any]] = []
    required = {
        "field_id",
        "quote",
        "slot",
        "source_interaction_seq",
        "status",
    }
    allowed = required | {"supports"}
    for index, item in enumerate(items):
        _validate_object_keys(
            item,
            field_name=f"evidence_candidates[{index}]",
            required=required,
            allowed=allowed,
        )
        candidate = {
            "field_id": strict_string(
                item["field_id"],
                field_name=f"evidence_candidates[{index}].field_id",
                maximum_length=256,
            ),
            "quote": strict_string(
                item["quote"],
                field_name=f"evidence_candidates[{index}].quote",
                maximum_length=8192,
            ),
            "slot": strict_string(
                item["slot"],
                field_name=f"evidence_candidates[{index}].slot",
                maximum_length=64,
            ),
            "source_interaction_seq": strict_nonnegative_int(
                item["source_interaction_seq"],
                field_name=(f"evidence_candidates[{index}].source_interaction_seq"),
            ),
            "status": strict_string(
                item["status"],
                field_name=f"evidence_candidates[{index}].status",
                maximum_length=32,
            ),
        }
        if "supports" in item:
            supports = strict_string(
                item["supports"],
                field_name=f"evidence_candidates[{index}].supports",
                maximum_length=64,
            )
            if supports not in _VALID_EVIDENCE_SUPPORT:
                raise ValueError(f"evidence_candidates[{index}].supports is invalid")
            candidate["supports"] = supports
        parsed.append(candidate)
    return parsed


def _object_list(
    value: Any,
    *,
    field_name: str,
    maximum_items: int = MAX_JSON_ARRAY_ITEMS,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array of objects")
    if len(value) > maximum_items:
        raise ValueError(f"{field_name} must contain at most {maximum_items} items")
    if any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{field_name} must be an array of objects")
    return [dict(item) for item in value]


def _validate_object_keys(
    value: dict[str, Any],
    *,
    field_name: str,
    required: set[str],
    allowed: set[str],
) -> None:
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} keys must be strings")
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(f"{field_name} is missing keys: {missing}")
    unknown = sorted(value.keys() - allowed)
    if unknown:
        raise ValueError(f"{field_name} contains unknown keys: {unknown}")
