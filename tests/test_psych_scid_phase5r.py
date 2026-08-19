import copy
import json

import pytest

from xtalk.psych_sop.scid.knowledge.phase5r import (
    PHASE5R_STATUS,
    SOURCE_NODE_IDS,
    Phase5RError,
    build_phase5r_artifacts,
    build_phase5r_review_record,
    compile_candidate_clinical_spec,
    validate_phase5r_candidate,
    validate_phase5r_review_record,
)


def _load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact_bytes(root):
    return {
        path.relative_to(root): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_phase5r_build_is_deterministic_and_preserves_the_frozen_baseline(tmp_path):
    first = build_phase5r_artifacts(
        output_root=tmp_path / "first-artifacts",
        workspace_dir=tmp_path / "first-workspace",
    )
    second = build_phase5r_artifacts(
        output_root=tmp_path / "second-artifacts",
        workspace_dir=tmp_path / "second-workspace",
    )

    assert _artifact_bytes(first) == _artifact_bytes(second)
    baseline = _load_json(first / "baseline" / "g-pilot-pre5r-baseline.json")
    assert baseline["counts"] == {
        "legacy_node_count": 11,
        "legacy_transition_count": 25,
        "legacy_candidate_count": 11,
        "legacy_review_item_count": 40,
    }


def test_phase5r_source_graph_excludes_engineering_nodes_and_stays_candidate_only(
    tmp_path,
):
    output = build_phase5r_artifacts(
        output_root=tmp_path / "artifacts", workspace_dir=tmp_path / "workspace"
    )
    candidate = _load_json(output / "candidates" / "g-pilot-5r-candidates.json")
    source_nodes = candidate["source_nodes"]

    assert candidate["candidate_status"] == PHASE5R_STATUS
    assert [node["node_id"] for node in source_nodes] == list(SOURCE_NODE_IDS)
    assert all("PROBE" not in node["node_id"] for node in source_nodes)
    assert all("SUMMARY" not in node["node_id"] for node in source_nodes)
    assert all("RETURN" not in node["node_id"] for node in source_nodes)

    spec = _load_json(output / "dist" / "g-pilot-clinical-spec-preview.json")
    assert spec["publication_status"] == PHASE5R_STATUS
    assert spec["compilation_status"] == "candidate_preview_not_compiled_validated"
    assert spec["internal_generated_controls"]["manual_source_control_node_count"] == 0


def test_phase5r_replay_keeps_uncertainty_and_conversation_plans_outside_cursor_state(
    tmp_path,
):
    output = build_phase5r_artifacts(
        output_root=tmp_path / "artifacts", workspace_dir=tmp_path / "workspace"
    )
    report = _load_json(output / "reports" / "g-pilot-5r-replay-report.json")
    traces = {item["case_id"]: item for item in report["traces"]}

    assert report["case_count"] == report["passed_case_count"] == 5
    assert report["failed_case_ids"] == []
    assert report["conversation_cursor_advance_count"] == 0
    assert report["illegal_effect_commit_count"] == 0
    assert traces["S9-uncertain-clarify-then-negative"]["visited_clinical_nodes"] == [
        "S9"
    ]
    assert (
        "unknown_score_stays_on_cursor"
        in traces["S9-uncertain-clarify-then-negative"]["rejected_events"]
    )
    assert (
        "illegal_proposed_effect_rejected"
        in traces["illegal-effect-is-rejected"]["rejected_events"]
    )
    assert set(traces["conversation-failure-stays-on-cursor"]["rejected_events"]) == {
        "conversation_timeout_stays_on_cursor",
        "conversation_empty_output_stays_on_cursor",
        "conversation_malformed_proposal_stays_on_cursor",
        "conversation_service_unavailable_stays_on_cursor",
    }

    equivalence = _load_json(
        output / "reports" / "g-pilot-pre5r-equivalence-report.json"
    )
    assert equivalence["case_count"] == equivalence["passed_case_count"] == 5
    assert equivalence["failed_case_ids"] == []
    assert "no Runtime Ledger mutation" in equivalence["comparison_scope"]


def test_phase5r_review_record_is_hash_bound_and_remains_a_review_gate(tmp_path):
    output = build_phase5r_artifacts(
        output_root=tmp_path / "artifacts", workspace_dir=tmp_path / "workspace"
    )
    candidate = _load_json(output / "candidates" / "g-pilot-5r-candidates.json")
    record = build_phase5r_review_record(candidate)

    validate_phase5r_review_record(record, candidate)
    assert record["review_summary"]["total_items"] == 16
    assert record["review_summary"]["item_status_counts"] == {"pending": 16}
    assert record["review_summary"]["clinical_review_gate_passed"] is False

    changed_candidate = copy.deepcopy(candidate)
    changed_candidate["limitations"].append("Changed after review record creation.")
    with pytest.raises(Phase5RError, match="hash is stale"):
        validate_phase5r_review_record(record, changed_candidate)


def test_phase5r_candidate_rejects_a_formal_unknown_transition(tmp_path):
    output = build_phase5r_artifacts(
        output_root=tmp_path / "artifacts", workspace_dir=tmp_path / "workspace"
    )
    candidate = _load_json(output / "candidates" / "g-pilot-5r-candidates.json")
    broken = copy.deepcopy(candidate)
    broken["source_nodes"][0]["formal_transitions"][0]["when"]["score_in"].append("?")

    with pytest.raises(Phase5RError, match="non-committed score"):
        validate_phase5r_candidate(broken)


def test_phase5r_compiler_converts_returns_to_internal_effects(tmp_path):
    output = build_phase5r_artifacts(
        output_root=tmp_path / "artifacts", workspace_dir=tmp_path / "workspace"
    )
    candidate = _load_json(output / "candidates" / "g-pilot-5r-candidates.json")
    spec = compile_candidate_clinical_spec(candidate)
    return_effects = [
        effect
        for node in spec["source_clinical_nodes"]
        for transition in node["transitions"]
        if transition["target"] == "$return"
        for effect in transition["effects"]
    ]

    assert return_effects
    assert {effect["type"] for effect in return_effects} == {"return_to_caller"}
