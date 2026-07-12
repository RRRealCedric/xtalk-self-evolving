"""Parsing and validation helpers for background SCID decisions."""

from __future__ import annotations

import json
from typing import Any

from .schema import AssessmentDecision, normalize_score


class DecisionParseError(ValueError):
    """Raised when a background-LM response cannot be parsed as a decision."""


def extract_json_object_text(text: str) -> str:
    """Extract one JSON object from model output text."""

    stripped = (text or "").strip()
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
        payload = json.loads(extract_json_object_text(text))
    except json.JSONDecodeError as exc:
        raise DecisionParseError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise DecisionParseError("Decision payload must be a JSON object")
    return decision_from_payload(payload)


def decision_from_payload(payload: dict[str, Any]) -> AssessmentDecision:
    """Build a typed decision from a JSON-like dict."""

    missing = [
        key
        for key in (
            "field_id",
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

    action = str(payload["next_action"]).strip()
    if action not in {"advance", "clarify", "reask", "branch", "crisis"}:
        raise DecisionParseError(f"Invalid next_action: {action!r}")

    score = None
    if payload.get("score") is not None and str(payload.get("score")).strip():
        score = normalize_score(payload.get("score"))

    evidence_raw = payload.get("evidence")
    if not isinstance(evidence_raw, list):
        raise DecisionParseError("evidence must be a list of strings")
    evidence = [str(item).strip() for item in evidence_raw if str(item).strip()]

    confidence = float(payload.get("confidence") or 0.0)
    confidence = max(0.0, min(1.0, confidence))
    return AssessmentDecision(
        field_id=str(payload.get("field_id") or "").strip(),
        score=score,
        confidence=confidence,
        evidence=evidence,
        next_action=action,  # type: ignore[arg-type]
        clarification_question=str(payload.get("clarification_question") or ""),
        reasoning_summary=str(payload.get("reasoning_summary") or ""),
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
