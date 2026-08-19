"""Build deterministic, redacted audit reports from persisted SCID facts.

This module is deliberately read-only with respect to a Runtime.  It consumes
the append-only event stream and optional final snapshot after a session has
run; it never reconstructs or mutates the Ledger.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


AUDIT_SCHEMA_VERSION = 1


class AuditValidationError(ValueError):
    """Raised when an episode cannot be projected safely."""


def load_episode_events(
    episode_dir: str | Path, episode_id: str
) -> list[dict[str, Any]]:
    """Read one legacy flat-layout event stream without interpreting raw text."""

    path = Path(episode_dir) / f"{episode_id}.events.jsonl"
    if not path.is_file():
        raise FileNotFoundError(path)
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditValidationError(
                    f"invalid JSON at {path.name}:{line_number}"
                ) from exc
            if not isinstance(event, dict):
                raise AuditValidationError(
                    f"event at {path.name}:{line_number} is not an object"
                )
            events.append(event)
    return events


def build_audit_projection(
    events: Iterable[Mapping[str, Any]],
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate redacted events into deterministic session and turn records."""

    normalized = [dict(event) for event in events]
    normalized.sort(key=lambda event: int(event.get("event_seq", 0)))
    continuity_errors = _continuity_errors(normalized)
    turns: dict[int, dict[str, Any]] = {}
    event_types: Counter[str] = Counter()
    model_calls: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    state_diffs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for event in normalized:
        event_type = str(event.get("event_type", "Unknown"))
        event_types[event_type] += 1
        payload = event.get("payload")
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        envelope = event.get("envelope")
        envelope = dict(envelope) if isinstance(envelope, Mapping) else {}
        interaction_seq = _interaction_seq(payload, envelope)
        turn = None
        if interaction_seq is not None:
            turn = turns.setdefault(
                interaction_seq,
                {
                    "interaction_seq": interaction_seq,
                    "event_seqs": [],
                    "events": [],
                    "model_calls": [],
                    "segments": [],
                    "state_diffs": [],
                    "errors": [],
                },
            )
            turn["event_seqs"].append(event.get("event_seq"))
            turn["events"].append(_event_view(event))

        if event_type in {
            "ModelCallCompleted",
            "ModelCallFailed",
            "ModelCallCancelled",
        }:
            record = {**_event_view(event), **payload}
            model_calls.append(record)
            if turn is not None:
                turn["model_calls"].append(record)
        elif event_type == "ForegroundSegmentGenerated":
            record = {**_event_view(event), **payload}
            segments.append(record)
            if turn is not None:
                turn["segments"].append(record)
        elif event_type == "StateDiffRecorded":
            record = {**_event_view(event), **payload}
            state_diffs.append(record)
            if turn is not None:
                turn["state_diffs"].append(record)
        elif event_type == "ForegroundSegmentDeliveryRecorded" and turn is not None:
            for segment in turn["segments"]:
                segment["delivery_status"] = payload.get(
                    "delivery_status", "unavailable"
                )
        if event_type == "OperationFailed":
            record = {**_event_view(event), **payload}
            errors.append(record)
            if turn is not None:
                turn["errors"].append(record)

    ordered_turns = [turns[key] for key in sorted(turns)]
    for turn in ordered_turns:
        turn["event_count"] = len(turn["events"])
        turn["model_call_count"] = len(turn["model_calls"])
        turn["segment_count"] = len(turn["segments"])
        turn["state_diff_count"] = len(turn["state_diffs"])

    return {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "event_count": len(normalized),
            "first_event_seq": normalized[0].get("event_seq") if normalized else None,
            "last_event_seq": normalized[-1].get("event_seq") if normalized else None,
            "event_sequence_continuous": not continuity_errors,
            "continuity_errors": continuity_errors,
        },
        "session": {
            "episode_id": _episode_id(normalized, snapshot),
            "runtime_profile": _snapshot_value(snapshot, "runtime_profile"),
            "terminal_status": _snapshot_value(snapshot, "status"),
            "raw_transcript_persisted": _snapshot_value(
                snapshot, "raw_transcript_persisted"
            ),
            "event_type_counts": dict(sorted(event_types.items())),
            "model_call_count": len(model_calls),
            "segment_count": len(segments),
            "state_diff_count": len(state_diffs),
            "error_count": len(errors),
        },
        "turns": ordered_turns,
        "model_calls": model_calls,
        "segments": segments,
        "state_diffs": state_diffs,
        "errors": errors,
    }


def render_audit_markdown(projection: Mapping[str, Any]) -> dict[str, str]:
    """Render a session index and one concise Markdown document per turn."""

    session = projection["session"]
    source = projection["source"]
    lines = [
        f"# SCID Gate 1 Audit: {session['episode_id'] or 'unknown'}",
        "",
        "## Session summary",
        f"- Audit schema: {projection['audit_schema_version']}",
        f"- Runtime profile: {session['runtime_profile'] or 'unavailable'}",
        f"- Terminal status: {session['terminal_status'] or 'unavailable'}",
        f"- Raw transcript persisted: {session['raw_transcript_persisted']}",
        f"- Events: {source['event_count']} (continuous: {source['event_sequence_continuous']})",
        f"- Model calls: {session['model_call_count']}; segments: {session['segment_count']}; state diffs: {session['state_diff_count']}; errors: {session['error_count']}",
        "",
        "## Turn index",
    ]
    for turn in projection["turns"]:
        seq = int(turn["interaction_seq"])
        lines.append(
            f"- [Turn {seq:04d}](turns/{seq:04d}.md): "
            f"{turn['event_count']} events, {turn['model_call_count']} model calls, "
            f"{turn['segment_count']} segments, {turn['state_diff_count']} state diffs"
        )
    if source["continuity_errors"]:
        lines.extend(["", "## Validation warnings"])
        lines.extend(f"- {error}" for error in source["continuity_errors"])

    rendered = {"session.md": "\n".join(lines) + "\n"}
    for turn in projection["turns"]:
        seq = int(turn["interaction_seq"])
        rendered[f"turns/{seq:04d}.md"] = _render_turn_markdown(turn)
    return rendered


def write_audit_report(
    episode_dir: str | Path,
    episode_id: str,
    *,
    output_dir: str | Path | None = None,
) -> Path:
    """Write a derived report tree. Existing source events are never changed."""

    root = Path(episode_dir)
    snapshot_path = root / f"{episode_id}.final.json"
    snapshot = (
        json.loads(snapshot_path.read_text(encoding="utf-8"))
        if snapshot_path.is_file()
        else None
    )
    projection = build_audit_projection(
        load_episode_events(root, episode_id), snapshot=snapshot
    )
    target = (
        Path(output_dir) if output_dir is not None else root / f"{episode_id}.audit"
    )
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    for relative, content in render_audit_markdown(projection).items():
        path = target / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)
    index = target / "index.json"
    index.write_text(
        json.dumps(projection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(index, 0o600)
    return target


def _render_turn_markdown(turn: Mapping[str, Any]) -> str:
    lines = [
        f"# Turn {int(turn['interaction_seq']):04d}",
        "",
        "## Privacy",
        "- Raw text is not rendered by this report.",
    ]
    for title, key, fields in (
        (
            "Model calls",
            "model_calls",
            ("component", "purpose", "status", "total_latency_ms", "request_id"),
        ),
        (
            "Foreground segments",
            "segments",
            (
                "segment_type",
                "source",
                "delivery_status",
                "character_count",
                "action_id",
            ),
        ),
        (
            "Ledger and state changes",
            "state_diffs",
            ("operation", "before", "decision", "after"),
        ),
        ("Errors", "errors", ("operation", "error_type", "error_code")),
    ):
        lines.extend(["", f"## {title}"])
        records = turn[key]
        if not records:
            lines.append("- No persisted record available.")
            continue
        for record in records:
            summary = "; ".join(
                f"{field}={record.get(field)!r}"
                for field in fields
                if record.get(field) is not None
            )
            lines.append(
                f"- event {record.get('event_seq')}: {summary or record.get('event_type')}"
            )
    lines.extend(["", "## Causal events"])
    lines.extend(
        f"- {event['event_seq']}: {event['event_type']}" for event in turn["events"]
    )
    return "\n".join(lines) + "\n"


def _event_view(event: Mapping[str, Any]) -> dict[str, Any]:
    envelope = event.get("envelope")
    return {
        "event_seq": event.get("event_seq"),
        "event_type": event.get("event_type"),
        "occurred_at": event.get("occurred_at"),
        "request_id": (
            envelope.get("request_id") if isinstance(envelope, Mapping) else None
        ),
        "correlation_id": (
            envelope.get("correlation_id") if isinstance(envelope, Mapping) else None
        ),
        "causation_id": (
            envelope.get("causation_id") if isinstance(envelope, Mapping) else None
        ),
    }


def _interaction_seq(
    payload: Mapping[str, Any], envelope: Mapping[str, Any]
) -> int | None:
    value = envelope.get("interaction_seq", payload.get("interaction_seq"))
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _continuity_errors(events: Sequence[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    expected = 1
    for event in events:
        actual = event.get("event_seq")
        if actual != expected:
            errors.append(f"expected event_seq {expected}, found {actual}")
            expected = (
                actual
                if isinstance(actual, int) and not isinstance(actual, bool)
                else expected
            )
        expected += 1
    return errors


def _snapshot_value(snapshot: Mapping[str, Any] | None, key: str) -> Any:
    return snapshot.get(key) if isinstance(snapshot, Mapping) else None


def _episode_id(
    events: Sequence[Mapping[str, Any]], snapshot: Mapping[str, Any] | None
) -> Any:
    if isinstance(snapshot, Mapping) and snapshot.get("episode_id"):
        return snapshot["episode_id"]
    return events[0].get("episode_id") if events else None
