"""Build non-published SCID candidate nodes from reviewed source-layer artifacts.

Candidate bundles are a review bridge, not a runtime bundle.  They preserve
the original structured Pilot alongside Phase 2/3 provenance so a reviewer can
approve, amend, or reject a proposal without silently changing released data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .loader import DEFAULT_SCID_KNOWLEDGE_DIR, load_knowledge_bundle
from .schema import TERMINAL_NODE_ID, InterviewNode


_PROJECT_ROOT = Path(__file__).resolve().parents[6]
DEFAULT_CANDIDATES_DIR = DEFAULT_SCID_KNOWLEDGE_DIR / "candidates"
DEFAULT_INVENTORY_PATH = (
    DEFAULT_SCID_KNOWLEDGE_DIR / "inventory" / "scid-5.acroform-inventory.json"
)
DEFAULT_PILOT_LOCATIONS_PATH = (
    DEFAULT_SCID_KNOWLEDGE_DIR / "inventory" / "scid-5.pilot-locations.json"
)
DEFAULT_OCR_BLOCKS_PATH = (
    DEFAULT_SCID_KNOWLEDGE_DIR / "source-map" / "g-pilot-ocr-blocks.json"
)
DEFAULT_OCR_QUALITY_PATH = (
    DEFAULT_SCID_KNOWLEDGE_DIR / "source-map" / "g-pilot-ocr-quality.json"
)
DEFAULT_ALIGNMENT_PATH = (
    DEFAULT_SCID_KNOWLEDGE_DIR / "source-map" / "g-pilot-anchor-alignment.json"
)

CANDIDATE_VERSION = "1.0.0"
CANDIDATE_STATUS = "candidate_only_not_published"
_PROJECT_OWNER_REVIEW_STATUS = "reported_complete_by_project_owner_2026-07-29"


class CandidateGenerationError(ValueError):
    """Raised when a source artifact cannot generate a reviewable candidate."""


def build_g_pilot_candidates(
    *,
    output_dir: str | Path = DEFAULT_CANDIDATES_DIR,
    inventory_path: str | Path = DEFAULT_INVENTORY_PATH,
    pilot_locations_path: str | Path = DEFAULT_PILOT_LOCATIONS_PATH,
    ocr_blocks_path: str | Path = DEFAULT_OCR_BLOCKS_PATH,
    ocr_quality_path: str | Path = DEFAULT_OCR_QUALITY_PATH,
    alignment_path: str | Path = DEFAULT_ALIGNMENT_PATH,
) -> Path:
    """Build deterministic G-Pilot candidate artifacts for subsequent review."""

    bundle = load_knowledge_bundle()
    inventory = _load_json_object(Path(inventory_path))
    locations = _load_json_object(Path(pilot_locations_path))
    ocr_blocks = _load_json_object(Path(ocr_blocks_path))
    ocr_quality = _load_json_object(Path(ocr_quality_path))
    alignment = _load_json_object(Path(alignment_path))
    source_sha256 = _validate_source_hashes(
        bundle.source_document,
        inventory=inventory,
        locations=locations,
        ocr_blocks=ocr_blocks,
        ocr_quality=ocr_quality,
        alignment=alignment,
    )

    candidates = _build_candidates(
        bundle.nodes.values(), locations=locations, alignment=alignment
    )
    candidate_payload = {
        "candidate_bundle_version": CANDIDATE_VERSION,
        "candidate_bundle_id": "scid5-zh-g-pilot-candidates",
        "candidate_status": CANDIDATE_STATUS,
        "source_document_sha256": source_sha256,
        "source_artifacts": {
            "inventory": _artifact_ref(Path(inventory_path)),
            "pilot_locations": _artifact_ref(Path(pilot_locations_path)),
            "ocr_blocks": _artifact_ref(Path(ocr_blocks_path)),
            "ocr_quality": _artifact_ref(Path(ocr_quality_path)),
            "anchor_alignment": _artifact_ref(Path(alignment_path)),
        },
        "review_handoff": {
            "phase_1_clinical_audit": _PROJECT_OWNER_REVIEW_STATUS,
            "phase_3_source_audit": _PROJECT_OWNER_REVIEW_STATUS,
            "detailed_reviewer_identity_or_item_overlay": "not_recorded_in_repository",
            "candidate_review": "pending",
            "release_constraint": "Candidate artifacts cannot be copied into a runtime bundle before a separate candidate/content review and release process.",
        },
        "candidates": candidates,
        "limitations": [
            "Candidate fields preserve proposed content from the bounded G Pilot; they are not newly released SCID content.",
            "OCR text and geometric proximity are source-review aids, not independent proof of clinical semantics.",
            "No candidate in this file grants a model authority to score, diagnose, or write a runtime ledger.",
        ],
    }
    validate_candidate_bundle(candidate_payload)
    report = _build_candidate_report(candidate_payload)
    review_template = _build_candidate_review_template(candidate_payload)

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    _write_json(destination / "g-pilot-candidates.json", candidate_payload)
    _write_json(destination / "g-pilot-candidate-report.json", report)
    _write_text(
        destination / "g-pilot-candidate-report.md",
        render_candidate_report(candidate_payload, report),
    )
    _write_json(destination / "g-pilot-candidate-review.json", review_template)
    return destination


def load_candidate_bundle(
    path: str | Path = DEFAULT_CANDIDATES_DIR / "g-pilot-candidates.json",
) -> dict[str, Any]:
    """Load the committed G-Pilot candidate bundle and validate its boundaries."""

    payload = _load_json_object(Path(path))
    validate_candidate_bundle(payload)
    return payload


def validate_candidate_bundle(payload: Mapping[str, Any]) -> None:
    """Validate that candidate data is traceable and remains non-published."""

    if payload.get("candidate_status") != CANDIDATE_STATUS:
        raise CandidateGenerationError("Candidate bundle must remain non-published")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise CandidateGenerationError("Candidate bundle must contain candidates")
    ids: set[str] = set()
    node_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise CandidateGenerationError("Candidate must be an object")
        candidate_id = _required_string(candidate.get("candidate_id"), "candidate_id")
        node_id = _required_string(
            candidate.get("candidate_node_id"), "candidate_node_id"
        )
        if candidate_id in ids or node_id in node_ids:
            raise CandidateGenerationError("Candidate IDs and node IDs must be unique")
        ids.add(candidate_id)
        node_ids.add(node_id)
        if candidate.get("candidate_status") != CANDIDATE_STATUS:
            raise CandidateGenerationError(
                f"Candidate {candidate_id} is not non-published"
            )
        source_evidence = candidate.get("source_evidence")
        if not isinstance(source_evidence, Mapping):
            raise CandidateGenerationError(
                f"Candidate {candidate_id} lacks source evidence"
            )
        if not source_evidence.get("knowledge_source_refs"):
            raise CandidateGenerationError(
                f"Candidate {candidate_id} lacks source refs"
            )
        proposal = candidate.get("proposal")
        if not isinstance(proposal, Mapping) or proposal.get("node_id") != node_id:
            raise CandidateGenerationError(
                f"Candidate {candidate_id} has an invalid proposal"
            )
        if not isinstance(candidate.get("review_items"), list):
            raise CandidateGenerationError(
                f"Candidate {candidate_id} lacks review items"
            )

    for candidate in candidates:
        for transition in candidate["proposal"].get("transition_candidates", []):
            target = transition.get("target_node_id")
            if target != TERMINAL_NODE_ID and target not in node_ids:
                raise CandidateGenerationError(
                    f"Candidate transition targets missing node: {target!r}"
                )


def render_candidate_report(
    candidate_bundle: Mapping[str, Any], report: Mapping[str, Any]
) -> str:
    """Render a deterministic review-oriented summary of candidate generation."""

    summary = report["summary"]
    conflict_rows = (
        "\n".join(
            f"| `{item['candidate_node_id']}` | `{item['kind']}` | {item['message']} |"
            for item in report["review_items"]
        )
        or "| — | — | No candidate-specific review items were generated. |"
    )
    return "\n".join(
        [
            "# G Pilot Candidate Generation Report",
            "",
            "## Status",
            "",
            f"- Candidate bundle: `{candidate_bundle['candidate_bundle_id']}`",
            f"- Status: `{candidate_bundle['candidate_status']}`",
            f"- Source SHA-256: `{candidate_bundle['source_document_sha256']}`",
            "- This report is a review handoff, not a released or runtime-executable bundle.",
            "",
            "## Summary",
            "",
            f"- Candidate nodes: {summary['candidate_node_count']}",
            f"- Candidate transitions: {summary['candidate_transition_count']}",
            f"- Natural-language expression candidates: {summary['expression_candidate_count']}",
            f"- Source confidence: {json.dumps(summary['source_confidence_counts'], ensure_ascii=False, sort_keys=True)}",
            f"- Candidate-specific review items: {summary['review_item_count']}",
            "",
            "## Candidate review items",
            "",
            "| Candidate node | Kind | Why it needs review |",
            "| --- | --- | --- |",
            conflict_rows,
            "",
            "## Required review decision",
            "",
            "For every candidate, confirm source evidence, semantic intent, evidence slots, score requirements, transition conditions, and any natural-language expression. Approval of a candidate does not publish it; release remains a later compiler and governance decision.",
            "",
        ]
    )


def _build_candidates(
    nodes: Iterable[InterviewNode],
    *,
    locations: Mapping[str, Any],
    alignment: Mapping[str, Any],
) -> list[dict[str, Any]]:
    location_by_node = {
        item.get("node_id"): item
        for item in locations.get("locations", [])
        if isinstance(item, Mapping) and isinstance(item.get("node_id"), str)
    }
    assets_by_node: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for asset in alignment.get("layout_assets", []):
        if not isinstance(asset, Mapping):
            continue
        for node_id in asset.get("node_ids", []):
            if isinstance(node_id, str):
                assets_by_node[node_id].append(asset)
    anchors_by_node: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for anchor in alignment.get("form_field_anchors", []):
        if isinstance(anchor, Mapping) and isinstance(anchor.get("node_id"), str):
            anchors_by_node[anchor["node_id"]].append(anchor)

    candidates: list[dict[str, Any]] = []
    for node in nodes:
        location = location_by_node.get(node.node_id, {})
        source_refs = [_source_ref_to_dict(item) for item in node.source_refs]
        anchors = sorted(
            (dict(item) for item in anchors_by_node.get(node.node_id, [])),
            key=lambda item: (item["page_number"], item["widget_object_ref"] or ""),
        )
        ocr_regions = [
            {
                "asset_id": asset["asset_id"],
                "pdf_page": asset["pdf_page"],
                "ocr_line_ids": asset["ocr_line_ids"],
            }
            for asset in assets_by_node.get(node.node_id, [])
        ]
        review_items = _candidate_review_items(node, location=location, anchors=anchors)
        candidates.append(
            {
                "candidate_id": f"scid5-zh-g-pilot-candidate:{node.node_id}",
                "candidate_node_id": node.node_id,
                "candidate_status": CANDIDATE_STATUS,
                "source_confidence": _source_confidence(node, location=location),
                "source_evidence": {
                    "knowledge_source_refs": source_refs,
                    "phase_2_location": dict(location),
                    "phase_3_ocr_regions": ocr_regions,
                    "phase_3_form_field_anchors": anchors,
                },
                "proposal": _node_proposal(node),
                "review_items": review_items,
            }
        )
    return candidates


def _node_proposal(node: InterviewNode) -> dict[str, Any]:
    expressions = []
    if node.canonical_prompt:
        expressions.append(
            {
                "expression_id": f"{node.node_id}.CANONICAL",
                "kind": "canonical_prompt_candidate",
                "text": node.canonical_prompt,
                "source_basis": "phase_1_canonical_prompt",
                "requires_candidate_review": node.node_type == "evidence_probe",
            }
        )
    return {
        "node_id": node.node_id,
        "module_id": node.module_id,
        "node_type": node.node_type,
        "clinical_intent_candidate": node.clinical_intent,
        "time_window_candidate": node.time_window,
        "evidence_slot_candidates": [
            {
                "slot_id": item.slot_id,
                "value_type": item.value_type,
                "description": item.description,
                "required_for_scores": list(item.required_for_scores),
            }
            for item in node.evidence_slots
        ],
        "score_requirement_candidates": [
            {
                "score": item.score,
                "required_slots": list(item.required_slots),
                "requires_assessor_judgment": item.requires_assessor_judgment,
                "summary": item.summary,
            }
            for item in node.score_requirements
        ],
        "dialogue_contract_candidate": {
            "core_concept": node.dialogue_contract.core_concept,
            "time_window": node.dialogue_contract.time_window,
            "severity_threshold": node.dialogue_contract.severity_threshold,
            "key_exclusions": list(node.dialogue_contract.key_exclusions),
            "required_information": list(node.dialogue_contract.required_information),
            "neutral_examples": list(node.dialogue_contract.neutral_examples),
            "allowed_paraphrases": list(node.dialogue_contract.allowed_paraphrases),
            "clarification_ladder": [
                {
                    "level": item.level,
                    "when_missing": list(item.when_missing),
                    "strategy": item.strategy,
                    "prompt_intent": item.prompt_intent,
                }
                for item in node.dialogue_contract.clarification_ladder
            ],
        },
        "natural_language_expression_candidates": expressions,
        "transition_candidates": [
            {
                "transition_id": item.transition_id,
                "when": dict(item.when),
                "target_node_id": item.target_node_id,
                "effects": [dict(effect) for effect in item.effects],
                "status": CANDIDATE_STATUS,
            }
            for item in node.transitions
        ],
    }


def _candidate_review_items(
    node: InterviewNode,
    *,
    location: Mapping[str, Any],
    anchors: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    items = [
        {
            "kind": "candidate_content_review",
            "severity": "required",
            "message": "Confirm the proposed semantics, slots, score requirements, transitions, and expressions before any release.",
        }
    ]
    if node.node_type == "evidence_probe":
        items.append(
            {
                "kind": "derived_clarification",
                "severity": "required",
                "message": "The clarification prompt is derived content and must retain non-leading, time-window-faithful wording.",
            }
        )
    if node.node_type in {"module_summary", "return"}:
        items.append(
            {
                "kind": "engineering_scaffold",
                "severity": "required",
                "message": "This is an engineering control node, not source clinical content; verify it remains non-diagnostic and non-user-visible where required.",
            }
        )
    if any(
        item.get("status") == "related_field_match"
        for item in location.get("source_refs", [])
    ):
        items.append(
            {
                "kind": "related_form_anchor",
                "severity": "required",
                "message": "The source region is linked through a related form anchor, not an exact AcroForm field; preserve this distinction.",
            }
        )
    if not anchors and node.node_type not in {"module_summary", "return"}:
        items.append(
            {
                "kind": "no_phase_3_field_widget",
                "severity": "review",
                "message": "No same-node form widget is available in the Pilot alignment; review the OCR/source-region evidence directly.",
            }
        )
    return items


def _source_confidence(node: InterviewNode, *, location: Mapping[str, Any]) -> str:
    if node.node_type in {"module_summary", "return"}:
        return "engineering_only"
    source_kinds = {item.kind for item in node.source_refs}
    statuses = {
        item.get("status")
        for item in location.get("source_refs", [])
        if isinstance(item, Mapping)
    }
    if "derived_clarification" in source_kinds or "related_field_match" in statuses:
        return "medium"
    if "exact_field_match" in statuses:
        return "high"
    return "medium"


def _build_candidate_report(candidate_bundle: Mapping[str, Any]) -> dict[str, Any]:
    candidates = candidate_bundle["candidates"]
    review_items = [
        {
            "candidate_node_id": candidate["candidate_node_id"],
            **item,
        }
        for candidate in candidates
        for item in candidate["review_items"]
    ]
    return {
        "candidate_report_version": CANDIDATE_VERSION,
        "candidate_bundle_sha256": _canonical_sha256(candidate_bundle),
        "candidate_status": CANDIDATE_STATUS,
        "summary": {
            "candidate_node_count": len(candidates),
            "candidate_transition_count": sum(
                len(candidate["proposal"]["transition_candidates"])
                for candidate in candidates
            ),
            "expression_candidate_count": sum(
                len(candidate["proposal"]["natural_language_expression_candidates"])
                for candidate in candidates
            ),
            "source_confidence_counts": dict(
                sorted(
                    Counter(
                        candidate["source_confidence"] for candidate in candidates
                    ).items()
                )
            ),
            "review_item_count": len(review_items),
            "review_item_kind_counts": dict(
                sorted(Counter(item["kind"] for item in review_items).items())
            ),
        },
        "review_items": review_items,
        "global_review_items": [
            {
                "kind": "audit_traceability",
                "severity": "required_before_release",
                "message": "Project-owner confirmations allow this candidate-generation step, but reviewer identity and per-item audit decisions are not recorded in this repository.",
            },
            {
                "kind": "publication_boundary",
                "severity": "required_before_release",
                "message": "Candidate-only data must not be copied into the released bundle or Runtime without the later review, compiler, and governance gates.",
            },
        ],
    }


def _build_candidate_review_template(
    candidate_bundle: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "review_id": "scid5-zh-g-pilot-candidate-review-v1",
        "candidate_bundle_id": candidate_bundle["candidate_bundle_id"],
        "candidate_bundle_sha256": _canonical_sha256(candidate_bundle),
        "status": "pending_candidate_review",
        "overall_decision": "pending",
        "decision_values": [
            "pending",
            "approve",
            "approve_with_changes",
            "reject",
            "needs_adjudication",
        ],
        "candidate_decisions": [
            {
                "candidate_id": candidate["candidate_id"],
                "candidate_node_id": candidate["candidate_node_id"],
                "source_decision": "pending",
                "semantics_decision": "pending",
                "slots_and_scores_decision": "pending",
                "transition_decision": "pending",
                "expression_decision": "pending",
                "notes": "",
            }
            for candidate in candidate_bundle["candidates"]
        ],
        "global_review_items": [
            "Confirm the Stage 3 audit record and attach detailed review evidence when it becomes available.",
            "Confirm that no candidate is published or used by Runtime before later gates are satisfied.",
        ],
    }


def _validate_source_hashes(
    bundle_source: Mapping[str, Any],
    *,
    inventory: Mapping[str, Any],
    locations: Mapping[str, Any],
    ocr_blocks: Mapping[str, Any],
    ocr_quality: Mapping[str, Any],
    alignment: Mapping[str, Any],
) -> str:
    expected = _required_string(
        bundle_source.get("sha256"), "bundle.source_document.sha256"
    )
    observed = {
        "inventory": inventory.get("source_document", {}).get("sha256"),
        "pilot_locations": locations.get("source_inventory_sha256"),
        "ocr_blocks": ocr_blocks.get("source_document_sha256"),
        "ocr_quality": ocr_quality.get("source_document_sha256"),
        "anchor_alignment": alignment.get("source_document_sha256"),
    }
    mismatched = {name: value for name, value in observed.items() if value != expected}
    if mismatched:
        raise CandidateGenerationError(
            f"Candidate source hashes do not match bundle source: {mismatched}"
        )
    return expected


def _source_ref_to_dict(source_ref: Any) -> dict[str, Any]:
    return {
        "source_id": source_ref.source_id,
        "kind": source_ref.kind,
        "locator": source_ref.locator,
        "description": source_ref.description,
        "pdf_page": source_ref.pdf_page,
        "printed_page": source_ref.printed_page,
        "field_id": source_ref.field_id,
    }


def _artifact_ref(path: Path) -> dict[str, str]:
    return {"path": _project_relative_path(path), "sha256": _sha256(path)}


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateGenerationError(f"Unable to read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise CandidateGenerationError(f"JSON document must be an object: {path}")
    return value


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CandidateGenerationError(f"Missing required string {name}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _project_relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(_PROJECT_ROOT))
    except ValueError:
        return str(path)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _write_text(path: Path, text: str) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(text, encoding="utf-8")
    os.replace(temporary_path, path)


def main() -> None:
    """Generate the non-published G-Pilot candidate review package."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_CANDIDATES_DIR)
    arguments = parser.parse_args()
    print(build_g_pilot_candidates(output_dir=arguments.output_dir))


if __name__ == "__main__":
    main()
