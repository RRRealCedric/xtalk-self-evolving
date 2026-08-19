"""Build and validate the Phase 5R SCID semantic/strategy separation artifacts.

Phase 5R is deliberately offline and candidate-only.  It migrates the bounded
pre-5R G pilot without changing its source files, review records, or Runtime.
The generated ClinicalSpec is a compiler preview, not an approved deployment
bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from .candidate_generation import DEFAULT_CANDIDATES_DIR, load_candidate_bundle
from .loader import (
    DEFAULT_SCID_KNOWLEDGE_DIR,
    load_knowledge_bundle,
    load_trajectory_fixtures,
)
from .schema import InterviewNode, VALID_SCORES


PHASE5R_VERSION = "0.1.0"
PHASE5R_STATUS = "candidate_only_not_published"
SOURCE_NODE_IDS = ("S9", "S12", "G3", "G6", "G7", "G11")
INTERNAL_TARGETS = frozenset({"$return", "$stay"})
SOURCE_NODE_TYPES = frozenset(
    {
        "screening_question",
        "criterion_question",
        "criterion_rule",
        "module_entry",
        "exclusion",
        "differential",
        "safety_gate",
        "terminal",
    }
)
PROVENANCE_RELATION_TYPES = frozenset(
    {
        "source_text",
        "source_region",
        "exact_field",
        "related_field",
        "ocr_region",
        "ocr_anchor",
    }
)

_PROJECT_ROOT = Path(__file__).resolve().parents[6]
DEFAULT_PHASE5R_DIR = DEFAULT_SCID_KNOWLEDGE_DIR / "phase5r"
DEFAULT_PHASE5R_WORKSPACE_DIR = (
    _PROJECT_ROOT
    / "psydata"
    / "realdata"
    / "PsychologySOP-Template"
    / "scid-review-packets"
    / "g-pilot-v0.1.0"
    / "phase-5r-source-review"
)


class Phase5RError(ValueError):
    """Raised when a Phase 5R artifact violates a semantic boundary."""


def build_phase5r_artifacts(
    *,
    output_root: str | Path = DEFAULT_PHASE5R_DIR,
    workspace_dir: str | Path = DEFAULT_PHASE5R_WORKSPACE_DIR,
) -> Path:
    """Build deterministic Phase 5R candidate, compiler-preview, and review artifacts.

    Parameters
    ----------
    output_root
        Destination for Phase 5R data artifacts.  This location is distinct
        from every pre-5R source and review file.
    workspace_dir
        Destination for the clinician-readable, hash-bound review workspace.

    Returns
    -------
    Path
        The artifact root after all generated artifacts have been validated.
    """

    destination = Path(output_root)
    bundle = load_knowledge_bundle()
    legacy_candidates = load_candidate_bundle()
    baseline = _build_pre5r_baseline(bundle, legacy_candidates)
    source_nodes = _build_source_nodes(bundle.nodes)
    provenance = _build_provenance(source_nodes, legacy_candidates)
    strategy_contract = _build_strategy_contract(source_nodes)
    candidate = {
        "phase5r_version": PHASE5R_VERSION,
        "candidate_bundle_id": "scid5-zh-g-pilot-5r-candidates",
        "candidate_status": PHASE5R_STATUS,
        "representation": "source-clinical-and-strategy-separated",
        "source_document": dict(bundle.source_document),
        "source_nodes": source_nodes,
        "provenance_relations": provenance,
        "strategy_contract": strategy_contract,
        "migration": {
            "pre5r_baseline_sha256": _canonical_sha256(baseline),
            "retained_source_node_ids": list(SOURCE_NODE_IDS),
            "migrated_dynamic_clarification": {
                "legacy_node_id": "S9.PROBE.OCCURRENCE",
                "new_representation": "ConversationPlan with semantic_anchor=S9 and missing_slots=[occurrence]",
            },
            "migrated_internal_controls": [
                "G.PILOT.OBSESSION_SUMMARY",
                "G.PILOT.OBSESSION_RETURN",
                "G.PILOT.COMPULSION_SUMMARY",
                "G.PILOT.COMPULSION_RETURN",
            ],
        },
        "limitations": [
            "This is a bounded G Pilot representation, not a complete G module or complete SCID-5-RV.",
            "The source clinical graph intentionally excludes derived clarification and internal summary/return nodes.",
            "No artifact in this directory authorizes diagnosis, Runtime use, or direct Ledger mutation.",
        ],
    }
    validate_phase5r_candidate(candidate)
    clinical_spec = compile_candidate_clinical_spec(candidate)
    fixtures = _build_replay_fixtures()
    replay_report = replay_phase5r_fixtures(clinical_spec, fixtures)
    equivalence_report = compare_pre5r_and_phase5r_fixtures(
        clinical_spec, load_trajectory_fixtures()
    )
    review_record = build_phase5r_review_record(candidate)
    validate_phase5r_review_record(review_record, candidate)
    schema = _phase5r_json_schema()

    _write_json(destination / "baseline" / "g-pilot-pre5r-baseline.json", baseline)
    _write_json(
        destination / "source-clinical" / "g-pilot-source-nodes.json",
        {"nodes": source_nodes},
    )
    _write_json(
        destination / "provenance" / "g-pilot-provenance-relations.json",
        {"relations": provenance},
    )
    _write_json(
        destination / "strategy-contracts" / "g-pilot-strategy-contract.json",
        strategy_contract,
    )
    _write_json(destination / "candidates" / "g-pilot-5r-candidates.json", candidate)
    _write_json(
        destination / "dist" / "g-pilot-clinical-spec-preview.json", clinical_spec
    )
    _write_json(destination / "fixtures" / "g-pilot-5r-replay-fixtures.json", fixtures)
    _write_json(
        destination / "reports" / "g-pilot-5r-replay-report.json", replay_report
    )
    _write_json(
        destination / "reports" / "g-pilot-pre5r-equivalence-report.json",
        equivalence_report,
    )
    _write_json(destination / "reviews" / "g-pilot-5r-review.json", review_record)
    _write_json(destination / "schema" / "phase5r.schema.json", schema)
    manifest = _build_manifest(destination)
    _write_json(destination / "phase5r-manifest.json", manifest)
    build_phase5r_review_workspace(
        candidate=candidate,
        review_record=review_record,
        output_dir=workspace_dir,
    )
    return destination


def validate_phase5r_candidate(candidate: Mapping[str, Any]) -> None:
    """Validate source-clinical and strategy objects without Runtime authority.

    Parameters
    ----------
    candidate
        The candidate-only Phase 5R representation.
    """

    if candidate.get("phase5r_version") != PHASE5R_VERSION:
        raise Phase5RError("Unsupported Phase 5R candidate version")
    if candidate.get("candidate_status") != PHASE5R_STATUS:
        raise Phase5RError("Phase 5R candidates must remain non-published")
    nodes = _require_list(candidate.get("source_nodes"), "source_nodes")
    if [node.get("node_id") for node in nodes] != list(SOURCE_NODE_IDS):
        raise Phase5RError("Phase 5R G Pilot must contain exactly six source nodes")
    relations = _require_list(
        candidate.get("provenance_relations"), "provenance_relations"
    )
    relation_ids = _validate_provenance_relations(relations)
    node_ids = {node["node_id"] for node in nodes if isinstance(node, Mapping)}
    for node in nodes:
        _validate_source_node(
            _require_mapping(node, "source_node"), node_ids, relation_ids
        )
    _validate_strategy_contract(
        _require_mapping(candidate.get("strategy_contract"), "strategy_contract"),
        node_ids,
    )


def compile_candidate_clinical_spec(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Compile a candidate-only ClinicalSpec preview from reviewed-shape source data.

    The compiler accepts no manually authored `summary`, `return`, or `probe`
    source nodes.  `$return` is converted to an explicitly internal engine
    effect; a missing final score is represented as a no-commit stay policy.
    """

    validate_phase5r_candidate(candidate)
    source_nodes = _require_list(candidate.get("source_nodes"), "source_nodes")
    compiled_nodes: list[dict[str, Any]] = []
    assessor_view: list[dict[str, Any]] = []
    strategy_view: list[dict[str, Any]] = []
    engine_view: list[dict[str, Any]] = []
    for node in source_nodes:
        source_node = _require_mapping(node, "source_node")
        node_id = _required_text(source_node.get("node_id"), "node_id")
        transitions = [
            _compile_transition(item, node_id)
            for item in source_node["formal_transitions"]
        ]
        compiled_nodes.append(
            {
                "node_id": node_id,
                "module_id": source_node["module_id"],
                "node_type": source_node["node_type"],
                "time_window": source_node["time_window"],
                "source_relation_ids": source_node["source_relation_ids"],
                "evidence_slots": source_node["evidence_slots"],
                "score_requirements": source_node["score_requirements"],
                "transitions": transitions,
                "no_commit_policy": {
                    "action": "stay_on_clinical_cursor",
                    "reason": "missing_or_uncommitted_evidence",
                    "missing_slots_from": "score_requirements",
                },
            }
        )
        assessor_view.append(
            {
                "node_id": node_id,
                "clinical_intent": source_node["clinical_intent"],
                "time_window": source_node["time_window"],
                "evidence_slots": source_node["evidence_slots"],
                "score_requirements": source_node["score_requirements"],
                "allowed_decisions": [
                    item["score"] for item in source_node["score_requirements"]
                ],
            }
        )
        constraints = source_node["semantic_constraints"]
        strategy_view.append(
            {
                "semantic_anchor": node_id,
                "clinical_intent": source_node["clinical_intent"],
                "time_window": source_node["time_window"],
                "required_information": constraints["required_information"],
                "semantic_constraints": [
                    constraints["core_concept"],
                    constraints["severity_threshold"],
                    *constraints["key_exclusions"],
                ],
                "prohibited_effects": [
                    "advance_clinical_cursor_without_committed_score",
                    "declare_diagnosis",
                    "write_ledger_directly",
                ],
            }
        )
        engine_view.append({"node_id": node_id, "transitions": transitions})

    result = {
        "clinical_spec_version": PHASE5R_VERSION,
        "clinical_spec_id": "scid5-zh-g-pilot-5r-preview",
        "compilation_status": "candidate_preview_not_compiled_validated",
        "publication_status": PHASE5R_STATUS,
        "candidate_bundle_sha256": _canonical_sha256(candidate),
        "source_document": candidate["source_document"],
        "entry_node_ids": ["S9", "S12"],
        "source_clinical_nodes": compiled_nodes,
        "views": {
            "assessor": assessor_view,
            "strategy": strategy_view,
            "engine": engine_view,
        },
        "internal_generated_controls": {
            "module_stack": True,
            "return_target": "$return",
            "no_commit_stays_on_current_cursor": True,
            "manual_source_control_node_count": 0,
        },
        "runtime_boundary": "adapter_preview_only_no_runtime_loader_or_ledger_write",
    }
    validate_clinical_spec_preview(result)
    return result


def validate_clinical_spec_preview(spec: Mapping[str, Any]) -> None:
    """Validate a compiled preview before it is used in replay only."""

    if spec.get("publication_status") != PHASE5R_STATUS:
        raise Phase5RError("ClinicalSpec preview must remain non-published")
    if spec.get("compilation_status") != "candidate_preview_not_compiled_validated":
        raise Phase5RError("Unexpected ClinicalSpec preview status")
    nodes = _require_list(spec.get("source_clinical_nodes"), "source_clinical_nodes")
    if {item.get("node_id") for item in nodes} != set(SOURCE_NODE_IDS):
        raise Phase5RError("ClinicalSpec preview must preserve the six source nodes")
    for node in nodes:
        if node.get("node_type") not in SOURCE_NODE_TYPES:
            raise Phase5RError("Compiler emitted a non-source clinical node")
        if node.get("node_type") in {"evidence_probe", "module_summary", "return"}:
            raise Phase5RError("Compiler emitted a pre-5R engineering node")
        for transition in _require_list(
            node.get("transitions"), "compiled.transitions"
        ):
            for effect in _require_list(transition.get("effects"), "compiled.effects"):
                if effect.get("type") not in {"module_push", "return_to_caller"}:
                    raise Phase5RError("Compiler emitted an unsupported formal effect")


def replay_phase5r_fixtures(
    spec: Mapping[str, Any], fixtures: Mapping[str, Any]
) -> dict[str, Any]:
    """Replay synthetic 5R cases through a non-writing adapter preview.

    Parameters
    ----------
    spec
        Candidate-only compiled ClinicalSpec preview.
    fixtures
        Synthetic replay scenarios that distinguish conversation focus from the
        formal clinical cursor.

    Returns
    -------
    dict[str, Any]
        Deterministic per-case traces and aggregate safety counters.
    """

    validate_clinical_spec_preview(spec)
    if fixtures.get("data_class") != "synthetic_only":
        raise Phase5RError("Phase 5R replay accepts synthetic fixtures only")
    nodes = {item["node_id"]: item for item in spec["source_clinical_nodes"]}
    traces: list[dict[str, Any]] = []
    for case in _require_list(fixtures.get("cases"), "fixtures.cases"):
        case_data = _require_mapping(case, "fixture.case")
        if case_data.get("data_class") != "synthetic":
            raise Phase5RError("Every Phase 5R fixture must be synthetic")
        traces.append(_replay_case(nodes, case_data))
    failures = [trace["case_id"] for trace in traces if not trace["passed"]]
    return {
        "replay_version": PHASE5R_VERSION,
        "clinical_spec_sha256": _canonical_sha256(spec),
        "fixture_set_sha256": _canonical_sha256(fixtures),
        "data_class": "synthetic_only",
        "case_count": len(traces),
        "passed_case_count": len(traces) - len(failures),
        "failed_case_ids": failures,
        "illegal_effect_commit_count": sum(
            trace["illegal_effect_commit_count"] for trace in traces
        ),
        "conversation_cursor_advance_count": sum(
            trace["conversation_cursor_advance_count"] for trace in traces
        ),
        "traces": traces,
    }


def compare_pre5r_and_phase5r_fixtures(
    spec: Mapping[str, Any], pre5r_fixtures: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Compare frozen pre-5R synthetic routes with 5R formal projections.

    Parameters
    ----------
    spec
        Candidate-only ClinicalSpec preview to replay.
    pre5r_fixtures
        Frozen synthetic trajectories from the pre-5R G Pilot.

    Returns
    -------
    dict[str, Any]
        Per-case route, committed-score, and internal-control comparison. This
        report deliberately does not simulate or mutate a Runtime Ledger.
    """

    validate_clinical_spec_preview(spec)
    nodes = {item["node_id"]: item for item in spec["source_clinical_nodes"]}
    fixture_list = list(pre5r_fixtures)
    traces: list[dict[str, Any]] = []
    for fixture in fixture_list:
        case = _require_mapping(fixture, "pre5r_fixture.case")
        if case.get("data_class") != "synthetic":
            raise Phase5RError("Pre-5R equivalence accepts synthetic fixtures only")
        projected_case, expected_projection = _project_pre5r_case(case)
        replay = _replay_case(nodes, projected_case)
        differences: list[str] = []
        if replay["visited_clinical_nodes"] != expected_projection["source_route"]:
            differences.append("source_route")
        if replay["final_cursor"] != "$return":
            differences.append("internal_return_control")
        if expected_projection["committed_scores"] != _committed_scores_from_events(
            projected_case["events"]
        ):
            differences.append("committed_score_projection")
        traces.append(
            {
                "case_id": case["case_id"],
                "passed": replay["passed"] and not differences,
                "pre5r_projection": expected_projection,
                "post5r_projection": {
                    "source_route": replay["visited_clinical_nodes"],
                    "committed_scores": _committed_scores_from_events(
                        projected_case["events"]
                    ),
                    "internal_return_control": replay["final_cursor"],
                },
                "differences": differences,
            }
        )
    failures = [trace["case_id"] for trace in traces if not trace["passed"]]
    return {
        "comparison_version": PHASE5R_VERSION,
        "comparison_scope": "synthetic formal score, source-route, and internal-control projection only; no Runtime Ledger mutation or equivalence claim",
        "pre5r_fixture_set_sha256": _canonical_sha256(fixture_list),
        "clinical_spec_sha256": _canonical_sha256(spec),
        "case_count": len(traces),
        "passed_case_count": len(traces) - len(failures),
        "failed_case_ids": failures,
        "traces": traces,
    }


def build_phase5r_review_record(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Create a new hash-bound review record for six source clinical nodes."""

    validate_phase5r_candidate(candidate)
    items: list[dict[str, Any]] = []
    for node in candidate["source_nodes"]:
        node_id = node["node_id"]
        items.extend(
            [
                _review_item(node_id, "source_and_semantics", "high", 2),
                _review_item(node_id, "scores_and_transitions", "high", 2),
            ]
        )
    items.extend(
        [
            _review_item("GLOBAL", "provenance_relations", "high", 1),
            _review_item("GLOBAL", "strategy_semantic_invariants", "routine", 1),
            _review_item(
                "ENGINE", "internal_return_and_stay_controls", "engineering", 1
            ),
            _review_item("ENGINE", "replay_and_effect_rejection", "engineering", 1),
        ]
    )
    summary = _review_summary(items)
    return {
        "review_record_version": PHASE5R_VERSION,
        "review_id": "scid5-zh-g-pilot-5r-source-review-v1",
        "record_status": "open",
        "candidate_bundle_id": candidate["candidate_bundle_id"],
        "candidate_bundle_sha256": _canonical_sha256(candidate),
        "candidate_status_required": PHASE5R_STATUS,
        "scope": "Six source clinical nodes only: S9, S12, G3, G6, G7, G11.",
        "explicitly_excluded": [
            "S9.PROBE.OCCURRENCE",
            "G.PILOT.*SUMMARY",
            "G.PILOT.*RETURN",
            "Every individual generated ConversationPlan utterance",
        ],
        "review_boundary": "Approval establishes only clinically_reviewed source content after all items pass; it does not mark this compiler preview compiled_validated, shadow_validated, or runtime_enabled.",
        "review_items": items,
        "review_summary": summary,
    }


def validate_phase5r_review_record(
    record: Mapping[str, Any], candidate: Mapping[str, Any]
) -> None:
    """Validate the immutable shape and hash binding of a 5R review record."""

    if record.get("review_record_version") != PHASE5R_VERSION:
        raise Phase5RError("Unsupported Phase 5R review record version")
    if record.get("candidate_bundle_sha256") != _canonical_sha256(candidate):
        raise Phase5RError("Phase 5R review record hash is stale")
    if record.get("candidate_status_required") != PHASE5R_STATUS:
        raise Phase5RError("Phase 5R review record must target non-published content")
    items = _require_list(record.get("review_items"), "review_items")
    expected_ids = {
        *(
            f"{node_id}.{kind}"
            for node_id in SOURCE_NODE_IDS
            for kind in ("source_and_semantics", "scores_and_transitions")
        ),
        "GLOBAL.provenance_relations",
        "GLOBAL.strategy_semantic_invariants",
        "ENGINE.internal_return_and_stay_controls",
        "ENGINE.replay_and_effect_rejection",
    }
    if {
        item.get("review_item_id") for item in items if isinstance(item, Mapping)
    } != expected_ids:
        raise Phase5RError("Phase 5R review record has an unexpected worklist")
    if record.get("review_summary") != _review_summary(items):
        raise Phase5RError("Phase 5R review summary is stale")


def build_phase5r_review_workspace(
    *,
    candidate: Mapping[str, Any],
    review_record: Mapping[str, Any],
    output_dir: str | Path = DEFAULT_PHASE5R_WORKSPACE_DIR,
) -> Path:
    """Render a source-oriented review workspace without publishing content."""

    validate_phase5r_candidate(candidate)
    validate_phase5r_review_record(review_record, candidate)
    output = Path(output_dir)
    node_by_id = {node["node_id"]: node for node in candidate["source_nodes"]}
    relation_by_id = {
        relation["relation_id"]: relation
        for relation in candidate["provenance_relations"]
    }
    markdown_lines = [
        "# SCID G Pilot 5R 来源临床审核包",
        "",
        "## 审核边界",
        "",
        f"- Candidate hash: `{_canonical_sha256(candidate)}`",
        "- 本包只审核六个来源临床节点，不审核旧 probe/summary/return 节点。",
        "- 对话策略审核语义不变量和 replay，不审核模型每一次生成的话术。",
        "- 本包不授权诊断、编译发布或 Runtime 接入。",
        "",
        "## 节点审核",
        "",
    ]
    for node_id in SOURCE_NODE_IDS:
        node = node_by_id[node_id]
        markdown_lines.extend(_render_source_node_markdown(node, relation_by_id))
    markdown_lines.extend(
        [
            "## 审核工作项",
            "",
            "| ID | 风险 | 需要的独立审批 | 当前状态 |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for item in review_record["review_items"]:
        markdown_lines.append(
            f"| `{item['review_item_id']}` | `{item['risk_level']}` | {item['required_independent_approvals']} | `{item['item_status']}` |"
        )
    markdown = "\n".join(markdown_lines) + "\n"
    rendered_html = (
        '<html><head><meta charset="utf-8"><title>SCID 5R Review</title></head><body><pre>'
        + html.escape(markdown)
        + "</pre></body></html>\n"
    )
    _write_text(output / "review-worklist.md", markdown)
    _write_text(output / "index.html", rendered_html)
    _write_json(
        output / "review-readiness.json",
        {
            "candidate_status": PHASE5R_STATUS,
            "candidate_bundle_sha256": _canonical_sha256(candidate),
            "review_summary": review_record["review_summary"],
            "ready_for_clinical_approval": False,
            "ready_for_runtime": False,
        },
    )
    _write_json(
        output / "workspace-manifest.json",
        {
            "workspace_version": PHASE5R_VERSION,
            "candidate_bundle_id": candidate["candidate_bundle_id"],
            "candidate_bundle_sha256": _canonical_sha256(candidate),
            "review_record_id": review_record["review_id"],
            "review_record_sha256": _canonical_sha256(review_record),
            "source_image_references": {
                "S9": "../images/scan-s9.png",
                "S12": "../images/scan-s12.png",
                "G3": "../images/g2-g3.png",
                "G6": "../images/g2-g6-g7.png",
                "G7": "../images/g2-g6-g7.png",
                "G11": "../images/g3-g11.png",
            },
            "publication_status": PHASE5R_STATUS,
        },
    )
    return output


def _build_pre5r_baseline(
    bundle: Any, legacy_candidates: Mapping[str, Any]
) -> dict[str, Any]:
    paths = {
        "legacy_module": DEFAULT_SCID_KNOWLEDGE_DIR / "modules" / "G" / "pilot.json",
        "legacy_candidate_bundle": DEFAULT_CANDIDATES_DIR / "g-pilot-candidates.json",
        "legacy_review_overlay": DEFAULT_CANDIDATES_DIR
        / "g-pilot-candidate-review-v2.json",
        "legacy_fixtures": DEFAULT_SCID_KNOWLEDGE_DIR
        / "fixtures"
        / "g_pilot_trajectories.json",
    }
    review_record = _load_json(paths["legacy_review_overlay"])
    transition_count = sum(len(node.transitions) for node in bundle.nodes.values())
    return {
        "baseline_version": PHASE5R_VERSION,
        "representation": "pre_5r_representation_baseline",
        "publication_status": PHASE5R_STATUS,
        "source_document_sha256": bundle.source_document["sha256"],
        "artifact_hashes": {
            label: {
                "path": str(path.relative_to(_PROJECT_ROOT)),
                "sha256": _sha256_file(path),
            }
            for label, path in paths.items()
        },
        "counts": {
            "legacy_node_count": len(bundle.nodes),
            "legacy_transition_count": transition_count,
            "legacy_candidate_count": len(legacy_candidates["candidates"]),
            "legacy_review_item_count": review_record["review_summary"]["total_items"],
        },
        "migration_exclusions": {
            "dynamic_clarification_node_ids": ["S9.PROBE.OCCURRENCE"],
            "internal_control_node_ids": [
                "G.PILOT.OBSESSION_SUMMARY",
                "G.PILOT.OBSESSION_RETURN",
                "G.PILOT.COMPULSION_SUMMARY",
                "G.PILOT.COMPULSION_RETURN",
            ],
        },
    }


def _build_source_nodes(nodes: Mapping[str, InterviewNode]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for node_id in SOURCE_NODE_IDS:
        node = nodes[node_id]
        node_type = "criterion_rule" if node_id == "G11" else node.node_type
        result.append(
            {
                "node_id": node.node_id,
                "module_id": node.module_id,
                "node_type": node_type,
                "clinical_intent": node.clinical_intent,
                "time_window": node.time_window,
                "canonical_prompt": node.canonical_prompt,
                "source_relation_ids": [],
                "evidence_slots": [
                    {
                        "slot_id": slot.slot_id,
                        "value_type": slot.value_type,
                        "description": slot.description,
                        "required_for_scores": list(slot.required_for_scores),
                    }
                    for slot in node.evidence_slots
                ],
                "score_requirements": [
                    {
                        "score": requirement.score,
                        "required_slots": list(requirement.required_slots),
                        "requires_assessor_judgment": requirement.requires_assessor_judgment,
                        "summary": requirement.summary,
                    }
                    for requirement in node.score_requirements
                ],
                "semantic_constraints": {
                    "core_concept": node.dialogue_contract.core_concept,
                    "severity_threshold": node.dialogue_contract.severity_threshold,
                    "key_exclusions": list(node.dialogue_contract.key_exclusions),
                    "required_information": list(
                        node.dialogue_contract.required_information
                    ),
                },
                "formal_transitions": _migrate_transitions(node),
                "coverage_status": "clinical_spec_candidate",
                "publication_status": PHASE5R_STATUS,
            }
        )
    return result


def _migrate_transitions(node: InterviewNode) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for transition in node.transitions:
        scores = transition.when.get("score_in")
        if not isinstance(scores, (tuple, list)):
            continue
        committed_scores = [score for score in scores if score != "?"]
        if not committed_scores:
            continue
        target = transition.target_node_id
        if target == "S9.PROBE.OCCURRENCE":
            continue
        if target not in SOURCE_NODE_IDS:
            target = "$return"
        result.append(
            {
                "transition_id": transition.transition_id,
                "when": {"score_in": committed_scores},
                "target": target,
            }
        )
    deduplicated: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
    for item in result:
        key = (tuple(item["when"]["score_in"]), item["target"])
        deduplicated.setdefault(key, item)
    return list(deduplicated.values())


def _build_provenance(
    source_nodes: list[dict[str, Any]], legacy_candidates: Mapping[str, Any]
) -> list[dict[str, Any]]:
    legacy_by_node = {
        item.get("candidate_node_id"): item
        for item in legacy_candidates["candidates"]
        if isinstance(item, Mapping)
    }
    relations: list[dict[str, Any]] = []
    for node in source_nodes:
        legacy = _require_mapping(
            legacy_by_node.get(node["node_id"]), "legacy_candidate"
        )
        evidence = _require_mapping(legacy.get("source_evidence"), "source_evidence")
        for index, source_ref in enumerate(
            _require_list(
                evidence.get("knowledge_source_refs"), "knowledge_source_refs"
            )
        ):
            ref = _require_mapping(source_ref, "knowledge_source_ref")
            kind = str(ref.get("kind"))
            relation_type = {
                "structured_scan_json": "source_text",
                "pdf_form_anchor": "exact_field",
                "pdf_region": "source_region",
            }.get(kind, "source_region")
            relations.append(
                _relation(
                    node["node_id"], relation_type, f"source-{index + 1}", dict(ref)
                )
            )
        location = _require_mapping(
            evidence.get("phase_2_location"), "phase_2_location"
        )
        for index, item in enumerate(
            _require_list(location.get("source_refs"), "phase_2_location.source_refs")
        ):
            source = _require_mapping(item, "phase_2_source_ref")
            status = source.get("status")
            relation_type = (
                "exact_field" if status == "exact_field_match" else "related_field"
            )
            relations.append(
                _relation(
                    node["node_id"], relation_type, f"phase2-{index + 1}", dict(source)
                )
            )
        for index, item in enumerate(
            _require_list(evidence.get("phase_3_ocr_regions"), "phase_3_ocr_regions")
        ):
            relations.append(
                _relation(
                    node["node_id"],
                    "ocr_region",
                    f"ocr-region-{index + 1}",
                    dict(_require_mapping(item, "ocr_region")),
                )
            )
        for index, item in enumerate(
            _require_list(
                evidence.get("phase_3_form_field_anchors"), "phase_3_form_field_anchors"
            )
        ):
            anchor = dict(_require_mapping(item, "ocr_anchor"))
            relation_type = (
                "exact_field"
                if anchor.get("match_status") == "exact_field_match"
                else "ocr_anchor"
            )
            relations.append(
                _relation(
                    node["node_id"], relation_type, f"ocr-anchor-{index + 1}", anchor
                )
            )
    unique: dict[str, dict[str, Any]] = {}
    for relation in relations:
        unique.setdefault(_canonical_sha256(relation), relation)
    result = list(unique.values())
    result.sort(key=lambda item: item["relation_id"])
    by_node: dict[str, list[str]] = {node["node_id"]: [] for node in source_nodes}
    for relation in result:
        by_node[relation["node_id"]].append(relation["relation_id"])
    for node in source_nodes:
        node["source_relation_ids"] = sorted(by_node[node["node_id"]])
    return result


def _relation(
    node_id: str, relation_type: str, suffix: str, evidence: Mapping[str, Any]
) -> dict[str, Any]:
    identity = f"{node_id}:{relation_type}:{suffix}:{_canonical_sha256(evidence)[:12]}"
    return {
        "relation_id": identity,
        "node_id": node_id,
        "relation_type": relation_type,
        "evidence": dict(evidence),
        "review_status": "source_mapped_pending_clinical_review",
    }


def _build_strategy_contract(
    source_nodes: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "strategy_contract_version": PHASE5R_VERSION,
        "scope": "Open ConversationPlan only; formal clinical effects require compiled preflight and Ledger commit.",
        "semantic_invariants": [
            "Do not alter a source node time window, criterion, severity threshold, or exclusion.",
            "Do not treat ordinary worry, preference, or background context as a clinical positive without source-supported evidence.",
            "Do not present model inference as a user statement or committed score.",
            "Unknown, contradiction, refusal, pause, stop, and safety takeover may retain the clinical cursor.",
        ],
        "bindings": [
            {
                "semantic_anchor": node["node_id"],
                "missing_slots": node["semantic_constraints"]["required_information"],
                "allowed_plan_fields": [
                    "utterance_goal",
                    "conversation_focus",
                    "candidate_evidence",
                    "proposed_effects",
                ],
                "prohibited_effects": [
                    "advance_clinical_cursor_without_committed_score",
                    "declare_diagnosis",
                    "write_ledger_directly",
                ],
            }
            for node in source_nodes
        ],
    }


def _compile_transition(transition: Mapping[str, Any], node_id: str) -> dict[str, Any]:
    target = transition["target"]
    effects: list[dict[str, str]] = []
    if target == "$return":
        effects.append({"type": "return_to_caller"})
    elif target in {"G3", "G11"} and node_id in {"S9", "S12"}:
        effects.append({"type": "module_push", "target_module": "G"})
    return {
        "transition_id": transition["transition_id"],
        "when": transition["when"],
        "target": target,
        "effects": effects,
    }


def _build_replay_fixtures() -> dict[str, Any]:
    return {
        "fixture_set_id": "scid5-zh-g-pilot-5r-replay",
        "fixture_version": PHASE5R_VERSION,
        "data_class": "synthetic_only",
        "cases": [
            {
                "case_id": "S9-uncertain-clarify-then-negative",
                "data_class": "synthetic",
                "entry_node_id": "S9",
                "events": [
                    {"kind": "assessment", "node_id": "S9", "score": "?"},
                    {
                        "kind": "conversation_plan",
                        "semantic_anchor": "S9",
                        "missing_slots": ["occurrence"],
                        "utterance_goal": "Clarify occurrence without repeating the full scan question.",
                    },
                    {"kind": "assessment", "node_id": "S9", "score": "1"},
                ],
                "expected": {
                    "visited_clinical_nodes": ["S9"],
                    "final_cursor": "$return",
                    "conversation_cursor_advance_count": 0,
                    "forbidden_clinical_nodes": ["S9.PROBE.OCCURRENCE", "G3"],
                },
            },
            {
                "case_id": "S9-positive-obsession-path",
                "data_class": "synthetic",
                "entry_node_id": "S9",
                "events": [
                    {"kind": "assessment", "node_id": "S9", "score": "3"},
                    {"kind": "assessment", "node_id": "G3", "score": "3"},
                    {"kind": "assessment", "node_id": "G6", "score": "3"},
                    {"kind": "assessment", "node_id": "G7", "score": "3"},
                ],
                "expected": {
                    "visited_clinical_nodes": ["S9", "G3", "G6", "G7"],
                    "final_cursor": "$return",
                    "conversation_cursor_advance_count": 0,
                    "forbidden_clinical_nodes": [
                        "G.PILOT.OBSESSION_SUMMARY",
                        "G.PILOT.OBSESSION_RETURN",
                    ],
                },
            },
            {
                "case_id": "S12-positive-gate-path",
                "data_class": "synthetic",
                "entry_node_id": "S12",
                "events": [
                    {"kind": "assessment", "node_id": "S12", "score": "3"},
                    {"kind": "assessment", "node_id": "G11", "score": "3"},
                ],
                "expected": {
                    "visited_clinical_nodes": ["S12", "G11"],
                    "final_cursor": "$return",
                    "conversation_cursor_advance_count": 0,
                    "forbidden_clinical_nodes": [
                        "G.PILOT.COMPULSION_SUMMARY",
                        "G.PILOT.COMPULSION_RETURN",
                    ],
                },
            },
            {
                "case_id": "illegal-effect-is-rejected",
                "data_class": "synthetic",
                "entry_node_id": "S9",
                "events": [
                    {
                        "kind": "conversation_plan",
                        "semantic_anchor": "S9",
                        "missing_slots": ["occurrence"],
                        "utterance_goal": "Acknowledge uncertainty.",
                        "proposed_effects": [
                            {"type": "advance_clinical_cursor", "target": "G6"}
                        ],
                    },
                    {"kind": "assessment", "node_id": "S9", "score": "1"},
                ],
                "expected": {
                    "visited_clinical_nodes": ["S9"],
                    "final_cursor": "$return",
                    "conversation_cursor_advance_count": 0,
                    "illegal_effect_commit_count": 0,
                },
            },
            {
                "case_id": "conversation-failure-stays-on-cursor",
                "data_class": "synthetic",
                "entry_node_id": "S9",
                "events": [
                    {"kind": "conversation_failure", "failure": "timeout"},
                    {"kind": "conversation_failure", "failure": "empty_output"},
                    {"kind": "conversation_failure", "failure": "malformed_proposal"},
                    {"kind": "conversation_failure", "failure": "service_unavailable"},
                    {"kind": "assessment", "node_id": "S9", "score": "1"},
                ],
                "expected": {
                    "visited_clinical_nodes": ["S9"],
                    "final_cursor": "$return",
                    "conversation_cursor_advance_count": 0,
                    "illegal_effect_commit_count": 0,
                    "required_rejected_events": [
                        "conversation_timeout_stays_on_cursor",
                        "conversation_empty_output_stays_on_cursor",
                        "conversation_malformed_proposal_stays_on_cursor",
                        "conversation_service_unavailable_stays_on_cursor",
                    ],
                },
            },
        ],
    }


def _replay_case(
    nodes: Mapping[str, Mapping[str, Any]], case: Mapping[str, Any]
) -> dict[str, Any]:
    cursor = _required_text(case.get("entry_node_id"), "fixture.entry_node_id")
    if cursor not in nodes:
        raise Phase5RError("Fixture starts at an unknown source node")
    visited = [cursor]
    conversation_focus: dict[str, Any] | None = None
    rejected: list[str] = []
    commits = 0
    conversation_cursor_advance_count = 0
    illegal_effect_commit_count = 0
    for event in _require_list(case.get("events"), "fixture.events"):
        item = _require_mapping(event, "fixture.event")
        kind = item.get("kind")
        if kind == "conversation_plan":
            if item.get("semantic_anchor") != cursor:
                rejected.append("conversation_plan_anchor_mismatch")
                continue
            if item.get("proposed_effects"):
                rejected.append("illegal_proposed_effect_rejected")
            conversation_focus = {
                "semantic_anchor": cursor,
                "missing_slots": list(item.get("missing_slots", [])),
                "utterance_goal": item.get("utterance_goal", ""),
            }
            continue
        if kind == "conversation_failure":
            failure = _required_text(
                item.get("failure"), "fixture.conversation_failure"
            )
            allowed_failures = {
                "timeout",
                "empty_output",
                "malformed_proposal",
                "service_unavailable",
            }
            if failure not in allowed_failures:
                raise Phase5RError("Fixture conversation failure is unsupported")
            rejected.append(f"conversation_{failure}_stays_on_cursor")
            continue
        if kind != "assessment":
            raise Phase5RError("Fixture event kind is unsupported")
        if item.get("node_id") != cursor:
            rejected.append("assessment_cursor_mismatch")
            continue
        score = _required_text(item.get("score"), "fixture.score")
        if score == "?":
            rejected.append("unknown_score_stays_on_cursor")
            continue
        transition = next(
            (
                candidate
                for candidate in nodes[cursor]["transitions"]
                if score in candidate["when"].get("score_in", [])
            ),
            None,
        )
        if transition is None:
            rejected.append("score_not_permitted_at_cursor")
            continue
        commits += 1
        cursor = transition["target"]
        if cursor in nodes:
            visited.append(cursor)
        conversation_focus = None
    expected = _require_mapping(case.get("expected"), "fixture.expected")
    passed = (
        visited == expected.get("visited_clinical_nodes")
        and cursor == expected.get("final_cursor")
        and conversation_cursor_advance_count
        == expected.get("conversation_cursor_advance_count", 0)
        and illegal_effect_commit_count
        == expected.get("illegal_effect_commit_count", 0)
        and not any(
            node in visited for node in expected.get("forbidden_clinical_nodes", [])
        )
        and set(expected.get("required_rejected_events", [])).issubset(rejected)
    )
    return {
        "case_id": case["case_id"],
        "passed": passed,
        "visited_clinical_nodes": visited,
        "final_cursor": cursor,
        "ledger_commit_count": commits,
        "conversation_focus": conversation_focus,
        "rejected_events": rejected,
        "conversation_cursor_advance_count": conversation_cursor_advance_count,
        "illegal_effect_commit_count": illegal_effect_commit_count,
    }


def _project_pre5r_case(
    case: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project one frozen legacy trajectory onto the 5R source graph."""

    steps = _require_list(case.get("steps"), "pre5r_fixture.steps")
    if not steps:
        raise Phase5RError("Pre-5R fixture cannot be empty")
    entry_node_id = _required_text(
        _require_mapping(steps[0], "pre5r_fixture.first_step").get("node_id"),
        "pre5r_fixture.entry_node_id",
    )
    if entry_node_id not in SOURCE_NODE_IDS:
        raise Phase5RError("Pre-5R fixture must start at a source clinical node")
    events: list[dict[str, Any]] = []
    source_route = [entry_node_id]
    committed_scores: list[dict[str, str]] = []
    for raw_step in steps:
        step = _require_mapping(raw_step, "pre5r_fixture.step")
        node_id = step.get("node_id")
        score = step.get("score")
        target_node_id: str | None = None
        if node_id in SOURCE_NODE_IDS:
            target_node_id = node_id
        elif node_id == "S9.PROBE.OCCURRENCE":
            target_node_id = "S9"
        else:
            continue
        if not isinstance(score, str):
            raise Phase5RError("Pre-5R source steps must contain a score")
        if source_route[-1] != target_node_id:
            source_route.append(target_node_id)
        if score == "?":
            events.extend(
                [
                    {"kind": "assessment", "node_id": target_node_id, "score": score},
                    {
                        "kind": "conversation_plan",
                        "semantic_anchor": target_node_id,
                        "missing_slots": ["occurrence"],
                        "utterance_goal": "Clarify the unresolved source evidence.",
                    },
                ]
            )
            continue
        events.append({"kind": "assessment", "node_id": target_node_id, "score": score})
        committed_scores.append({"node_id": target_node_id, "score": score})
    projected_case = {
        "case_id": case["case_id"],
        "data_class": "synthetic",
        "entry_node_id": entry_node_id,
        "events": events,
        "expected": {
            "visited_clinical_nodes": source_route,
            "final_cursor": "$return",
            "conversation_cursor_advance_count": 0,
            "illegal_effect_commit_count": 0,
        },
    }
    return projected_case, {
        "source_route": source_route,
        "committed_scores": committed_scores,
        "internal_return_control": "$return",
    }


def _committed_scores_from_events(
    events: Iterable[Mapping[str, Any]]
) -> list[dict[str, str]]:
    """Extract only committed formal-score proposals from a projected fixture."""

    result: list[dict[str, str]] = []
    for event in events:
        if event.get("kind") != "assessment" or event.get("score") == "?":
            continue
        result.append(
            {
                "node_id": _required_text(event.get("node_id"), "event.node_id"),
                "score": _required_text(event.get("score"), "event.score"),
            }
        )
    return result


def _review_item(node_id: str, kind: str, risk: str, approvals: int) -> dict[str, Any]:
    return {
        "review_item_id": f"{node_id}.{kind}",
        "risk_level": risk,
        "required_independent_approvals": approvals,
        "reviewer_role": (
            "clinical_reviewer" if risk != "engineering" else "engineering_reviewer"
        ),
        "release_blocking": True,
        "item_status": "pending",
        "reviewer_decisions": [],
        "adjudication": {"status": "not_required"},
    }


def _review_summary(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    item_list = list(items)
    counts = Counter(str(item.get("item_status")) for item in item_list)
    blockers = [
        item["review_item_id"]
        for item in item_list
        if item.get("item_status") != "approved" and item.get("release_blocking")
    ]
    return {
        "total_items": len(item_list),
        "item_status_counts": dict(sorted(counts.items())),
        "clinical_review_gate_passed": not blockers,
        "open_blocking_item_ids": blockers,
        "candidate_stays_non_published": True,
    }


def _validate_provenance_relations(relations: Iterable[Any]) -> set[str]:
    relation_ids: set[str] = set()
    for item in relations:
        relation = _require_mapping(item, "provenance_relation")
        relation_id = _required_text(relation.get("relation_id"), "relation_id")
        if relation_id in relation_ids:
            raise Phase5RError("Duplicate provenance relation id")
        relation_ids.add(relation_id)
        if relation.get("relation_type") not in PROVENANCE_RELATION_TYPES:
            raise Phase5RError("Unsupported provenance relation type")
        if relation.get("node_id") not in SOURCE_NODE_IDS:
            raise Phase5RError("Provenance relation points outside the source graph")
        _require_mapping(relation.get("evidence"), "provenance.evidence")
    return relation_ids


def _validate_source_node(
    node: Mapping[str, Any], node_ids: set[str], relation_ids: set[str]
) -> None:
    node_id = _required_text(node.get("node_id"), "source_node.node_id")
    if node.get("node_type") not in SOURCE_NODE_TYPES:
        raise Phase5RError(f"Node {node_id} has an invalid source clinical type")
    if node.get("node_type") in {"evidence_probe", "module_summary", "return"}:
        raise Phase5RError(f"Node {node_id} is a prohibited pre-5R engineering node")
    ids = _require_list(node.get("source_relation_ids"), "source_relation_ids")
    if not ids or not set(ids).issubset(relation_ids):
        raise Phase5RError(f"Node {node_id} has unresolved provenance")
    slots = _require_list(node.get("evidence_slots"), "evidence_slots")
    slot_ids = {
        _required_text(item.get("slot_id"), "slot_id")
        for item in slots
        if isinstance(item, Mapping)
    }
    if len(slot_ids) != len(slots):
        raise Phase5RError(f"Node {node_id} has duplicate or invalid slots")
    for requirement in _require_list(
        node.get("score_requirements"), "score_requirements"
    ):
        item = _require_mapping(requirement, "score_requirement")
        if item.get("score") not in VALID_SCORES - {"?"}:
            raise Phase5RError(f"Node {node_id} has an unsupported committed score")
        if not set(
            _require_list(item.get("required_slots"), "required_slots")
        ).issubset(slot_ids):
            raise Phase5RError(f"Node {node_id} score requirement uses an unknown slot")
    constraints = _require_mapping(
        node.get("semantic_constraints"), "semantic_constraints"
    )
    if not set(
        _require_list(constraints.get("required_information"), "required_information")
    ).issubset(slot_ids):
        raise Phase5RError(f"Node {node_id} strategy contract uses an unknown slot")
    transitions = _require_list(node.get("formal_transitions"), "formal_transitions")
    if not transitions:
        raise Phase5RError(f"Node {node_id} must have a formal transition")
    for transition in transitions:
        item = _require_mapping(transition, "formal_transition")
        if set(_require_mapping(item.get("when"), "transition.when")) != {"score_in"}:
            raise Phase5RError("Source transitions must be score-based formal rules")
        scores = _require_list(item["when"].get("score_in"), "transition.score_in")
        if not scores or not set(scores).issubset(VALID_SCORES - {"?"}):
            raise Phase5RError("Source transition includes a non-committed score")
        if item.get("target") not in node_ids | INTERNAL_TARGETS:
            raise Phase5RError("Source transition target is not clinically resolvable")


def _validate_strategy_contract(
    contract: Mapping[str, Any], node_ids: set[str]
) -> None:
    bindings = _require_list(contract.get("bindings"), "strategy_contract.bindings")
    if {
        item.get("semantic_anchor") for item in bindings if isinstance(item, Mapping)
    } != node_ids:
        raise Phase5RError("Strategy bindings must match source clinical nodes")
    for binding in bindings:
        item = _require_mapping(binding, "strategy_binding")
        if "advance_clinical_cursor_without_committed_score" not in _require_list(
            item.get("prohibited_effects"), "prohibited_effects"
        ):
            raise Phase5RError(
                "Strategy binding must prohibit uncommitted cursor advance"
            )


def _render_source_node_markdown(
    node: Mapping[str, Any], relations: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    lines = [
        f"### {node['node_id']}",
        "",
        f"- 类型：`{node['node_type']}`",
        f"- 临床意图：{node['clinical_intent']}",
        f"- 时间范围：`{node['time_window']}`",
    ]
    if node["canonical_prompt"]:
        lines.extend(["", "#### 来源原题", "", f"> {node['canonical_prompt']}"])
    lines.extend(["", "#### 来源关系", ""])
    for relation_id in node["source_relation_ids"]:
        relation = relations[relation_id]
        lines.append(
            f"- `{relation['relation_type']}`：`{json.dumps(relation['evidence'], ensure_ascii=False, sort_keys=True)}`"
        )
    lines.extend(
        [
            "",
            "#### 临床语义与正式规则",
            "",
            f"- 核心概念：{node['semantic_constraints']['core_concept']}",
            f"- 阈值边界：{node['semantic_constraints']['severity_threshold']}",
            f"- 排除/反例：{'；'.join(node['semantic_constraints']['key_exclusions']) or '无'}",
            "",
            "| Score | 所需 slot | 说明 |",
            "| --- | --- | --- |",
        ]
    )
    for requirement in node["score_requirements"]:
        lines.append(
            f"| `{requirement['score']}` | {', '.join(requirement['required_slots'])} | {requirement['summary']} |"
        )
    return lines + [""]


def _build_manifest(root: Path) -> dict[str, Any]:
    artifact_paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name != "phase5r-manifest.json"
    )
    return {
        "phase5r_version": PHASE5R_VERSION,
        "status": PHASE5R_STATUS,
        "artifact_hashes": [
            {"path": str(path.relative_to(root)), "sha256": _sha256_file(path)}
            for path in artifact_paths
        ],
        "runtime_boundary": "No Runtime integration; adapter preview/replay only.",
    }


def _phase5r_json_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "SCID Phase 5R semantic/strategy separation",
        "type": "object",
        "$defs": {
            "SourceClinicalNode": {
                "type": "object",
                "required": [
                    "node_id",
                    "node_type",
                    "source_relation_ids",
                    "evidence_slots",
                    "score_requirements",
                    "formal_transitions",
                ],
                "properties": {"node_type": {"enum": sorted(SOURCE_NODE_TYPES)}},
            },
            "ProvenanceRelation": {
                "type": "object",
                "required": ["relation_id", "node_id", "relation_type", "evidence"],
                "properties": {
                    "relation_type": {"enum": sorted(PROVENANCE_RELATION_TYPES)}
                },
            },
            "ConversationPlan": {
                "type": "object",
                "required": ["utterance_goal", "conversation_focus", "semantic_anchor"],
                "properties": {"proposed_effects": {"type": "array"}},
            },
            "CandidateEvidence": {
                "type": "object",
                "required": ["semantic_anchor", "source_turn_ids", "interpretation"],
            },
            "ProposedEffect": {
                "type": "object",
                "required": ["type", "semantic_anchor"],
                "properties": {
                    "type": {
                        "enum": [
                            "commit_score",
                            "module_push",
                            "module_pop",
                            "pause",
                            "stop",
                            "safety_takeover",
                        ]
                    }
                },
            },
        },
    }


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return dict(_require_mapping(value, f"JSON object at {path}"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Phase5RError(f"{name} must be an object")
    return value


def _require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise Phase5RError(f"{name} must be a list")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Phase5RError(f"{name} must be a non-empty string")
    return value.strip()


def main() -> None:
    """Build Phase 5R artifacts from the frozen pre-5R G Pilot."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_PHASE5R_DIR)
    parser.add_argument(
        "--workspace-dir", type=Path, default=DEFAULT_PHASE5R_WORKSPACE_DIR
    )
    args = parser.parse_args()
    build_phase5r_artifacts(
        output_root=args.output_root, workspace_dir=args.workspace_dir
    )


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
