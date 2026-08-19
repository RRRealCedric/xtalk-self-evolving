"""Parsing and validation helpers for background SCID decisions."""

from __future__ import annotations

from typing import Any

from ..core.schema import AssessmentDecision, normalize_score
from ..core.validation import (
    MAX_MODEL_JSON_BYTES,
    strict_finite_float,
    strict_json_loads,
    strict_string,
    strict_string_list,
)


class DecisionParseError(ValueError):
    """Raised when a background-LM response cannot be parsed as a decision."""


def extract_json_object_text(text: str) -> str:
    """Extract one JSON object from model output text."""

    if not isinstance(text, str):
        raise DecisionParseError("Model output must be text")
    if len(text.encode("utf-8")) > MAX_MODEL_JSON_BYTES:
        raise DecisionParseError(f"Model output exceeds {MAX_MODEL_JSON_BYTES} bytes")
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    raise DecisionParseError("No JSON object found in model output")


def parse_assessment_decision(text: str) -> AssessmentDecision:
    """Parse model text into an ``AssessmentDecision``."""

    try:
        payload = strict_json_loads(extract_json_object_text(text))
    except DecisionParseError:
        raise
    except ValueError as exc:
        raise DecisionParseError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise DecisionParseError("Decision payload must be a JSON object")
    return decision_from_payload(payload)


def decision_from_payload(payload: dict[str, Any]) -> AssessmentDecision:
    """Build a typed decision from a JSON-like dict."""

    if not isinstance(payload, dict):
        raise DecisionParseError("Decision payload must be a JSON object")
    if any(not isinstance(key, str) for key in payload):
        raise DecisionParseError("Decision payload keys must be strings")
    allowed = {
        "field_id",
        "score",
        "confidence",
        "evidence",
        "next_action",
        "clarification_question",
        "reasoning_summary",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise DecisionParseError(f"Decision payload contains unknown keys: {unknown}")
    missing = [
        key
        for key in (
            "field_id",
            "score",
            "confidence",
            "evidence",
            "next_action",
            "clarification_question",
            "reasoning_summary",
        )
        if key not in payload
    ]
    if missing:
        raise DecisionParseError(f"Decision payload missing keys: {missing}")

    try:
        action = strict_string(
            payload["next_action"],
            field_name="next_action",
            maximum_length=32,
        )
    except ValueError as exc:
        raise DecisionParseError(str(exc)) from exc
    if action not in {"advance", "clarify", "reask", "branch", "crisis"}:
        raise DecisionParseError(f"Invalid next_action: {action!r}")

    score = None
    if payload.get("score") is not None:
        try:
            score_text = strict_string(
                payload["score"],
                field_name="score",
                maximum_length=32,
            )
            score = normalize_score(score_text)
        except ValueError as exc:
            raise DecisionParseError(str(exc)) from exc

    try:
        evidence = strict_string_list(
            payload["evidence"],
            field_name="evidence",
            maximum_items=32,
            maximum_item_length=4096,
        )
        confidence = strict_finite_float(
            payload["confidence"],
            field_name="confidence",
            minimum=0.0,
            maximum=1.0,
        )
        field_id = strict_string(
            payload["field_id"],
            field_name="field_id",
            maximum_length=256,
        )
        clarification_question = strict_string(
            payload["clarification_question"],
            field_name="clarification_question",
            maximum_length=4096,
            allow_empty=True,
        )
        reasoning_summary = strict_string(
            payload["reasoning_summary"],
            field_name="reasoning_summary",
            maximum_length=8192,
            allow_empty=True,
        )
    except ValueError as exc:
        raise DecisionParseError(str(exc)) from exc

    return AssessmentDecision(
        field_id=field_id,
        score=score,
        confidence=confidence,
        evidence=evidence,
        next_action=action,  # type: ignore[arg-type]
        clarification_question=clarification_question,
        reasoning_summary=reasoning_summary,
        raw_payload=dict(payload),
    )


def fallback_reask_decision(
    *,
    field_id: str,
    reason: str,
    question: str = "我还需要多确认一点。你能结合刚才这个问题再具体说说吗？",
) -> AssessmentDecision:
    """Return a safe reask decision when parsing or validation fails."""

    return AssessmentDecision(
        field_id=field_id,
        score=None,
        confidence=0.0,
        evidence=[],
        next_action="reask",
        clarification_question=question,
        reasoning_summary=reason,
        raw_payload={"fallback": True, "reason": reason},
    )
