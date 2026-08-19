import copy
import json

import pytest

from xtalk.psych_sop.scid.knowledge.candidate_generation import load_candidate_bundle
from xtalk.psych_sop.scid.knowledge.candidate_review import (
    CandidateReviewError,
    build_g_pilot_review_workspace,
    build_review_record,
    refresh_review_summary,
    validate_candidate_review_record,
)


def test_phase_five_review_record_is_hash_bound_and_open():
    candidate = load_candidate_bundle()
    record = build_review_record(candidate)

    summary = validate_candidate_review_record(record, candidate)

    assert summary["total_items"] == 40
    assert summary["item_status_counts"] == {"pending": 40}
    assert summary["ready_for_compiler"] is False
    assert summary["candidate_stays_non_published"] is True
    assert any(
        item["review_item_id"] == "S9.PROBE.OCCURRENCE.expression"
        and item["required_approvals"]
        == [{"reviewer_role": "clinical_reviewer", "minimum_independent_approvals": 2}]
        for item in record["review_items"]
    )


def test_phase_five_review_record_rejects_a_candidate_hash_change():
    candidate = load_candidate_bundle()
    record = build_review_record(candidate)
    changed_candidate = copy.deepcopy(candidate)
    changed_candidate["limitations"].append("Changed after review creation.")

    with pytest.raises(CandidateReviewError, match="hash does not match"):
        validate_candidate_review_record(record, changed_candidate)


def test_phase_five_requires_distinct_reviewers_for_double_review():
    candidate = load_candidate_bundle()
    record = build_review_record(candidate)
    review_item = next(
        item
        for item in record["review_items"]
        if item["review_item_id"] == "S9.slots_and_scores"
    )
    review_item["reviewer_decisions"] = [
        {
            "reviewer_id": "clinical-a",
            "reviewer_role": "clinical_reviewer",
            "decision": "approve",
            "reviewed_on": "2026-07-29",
            "evidence_note": "Checked the linked source and the score-evidence distinction.",
            "proposed_change": "",
        },
        {
            "reviewer_id": "clinical-a",
            "reviewer_role": "clinical_reviewer",
            "decision": "approve",
            "reviewed_on": "2026-07-29",
            "evidence_note": "Repeated reviewer identifier intentionally used for this test.",
            "proposed_change": "",
        },
    ]
    record = refresh_review_summary(record)

    with pytest.raises(CandidateReviewError, match="duplicate reviewer_id"):
        validate_candidate_review_record(record, candidate)


def test_phase_five_resolved_adjudication_has_a_single_final_outcome():
    candidate = load_candidate_bundle()
    record = build_review_record(candidate)
    review_item = next(
        item
        for item in record["review_items"]
        if item["review_item_id"] == "S9.expression"
    )
    review_item["reviewer_decisions"] = [
        {
            "reviewer_id": "clinical-a",
            "reviewer_role": "clinical_reviewer",
            "decision": "reject",
            "reviewed_on": "2026-07-29",
            "evidence_note": "The item was escalated for an explicit adjudication test.",
            "proposed_change": "Clarify the expression decision in the adjudication record.",
        }
    ]
    review_item["adjudication"] = {
        "status": "resolved",
        "adjudicator_id": "clinical-chair",
        "decided_on": "2026-07-29",
        "decision": "approve",
        "rationale": "The reviewed candidate is retained unchanged for this test.",
    }
    record = refresh_review_summary(record)

    validate_candidate_review_record(record, candidate)
    refreshed_item = next(
        item
        for item in record["review_items"]
        if item["review_item_id"] == "S9.expression"
    )
    assert refreshed_item["item_status"] == "approved"


def test_phase_five_workspace_is_readable_and_does_not_publish_candidates(tmp_path):
    candidate = load_candidate_bundle()
    record = build_review_record(candidate)
    record_path = tmp_path / "review.json"
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    output = build_g_pilot_review_workspace(
        review_record_path=record_path, output_dir=tmp_path / "workspace"
    )

    worklist = (output / "review-worklist.md").read_text(encoding="utf-8")
    readiness = json.loads(
        (output / "review-readiness.json").read_text(encoding="utf-8")
    )
    assert "待审语义项" in worklist
    assert "candidate_only_not_published" in worklist
    assert readiness["candidate_stays_non_published"] is True
