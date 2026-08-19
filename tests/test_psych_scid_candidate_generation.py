import json

import pytest

from xtalk.psych_sop.scid.knowledge.candidate_generation import (
    CANDIDATE_STATUS,
    CandidateGenerationError,
    DEFAULT_CANDIDATES_DIR,
    build_g_pilot_candidates,
    load_candidate_bundle,
    validate_candidate_bundle,
)


def test_committed_candidate_bundle_is_traceable_and_not_published():
    bundle = load_candidate_bundle()

    assert bundle["candidate_status"] == CANDIDATE_STATUS
    assert len(bundle["candidates"]) == 11
    assert all(
        candidate["candidate_status"] == CANDIDATE_STATUS
        and candidate["source_evidence"]["knowledge_source_refs"]
        for candidate in bundle["candidates"]
    )

    g3 = next(
        item for item in bundle["candidates"] if item["candidate_node_id"] == "G3"
    )
    assert g3["source_confidence"] == "medium"
    assert any(item["kind"] == "related_form_anchor" for item in g3["review_items"])
    assert g3["proposal"]["transition_candidates"][0]["target_node_id"] == "G6"


def test_candidate_generation_is_deterministic_and_keeps_review_overlay_separate(
    tmp_path,
):
    output = build_g_pilot_candidates(output_dir=tmp_path)

    for filename in (
        "g-pilot-candidates.json",
        "g-pilot-candidate-report.json",
        "g-pilot-candidate-report.md",
        "g-pilot-candidate-review.json",
    ):
        assert (output / filename).read_bytes() == (
            DEFAULT_CANDIDATES_DIR / filename
        ).read_bytes()

    review = json.loads((output / "g-pilot-candidate-review.json").read_text())
    assert review["status"] == "pending_candidate_review"
    assert len(review["candidate_decisions"]) == 11


def test_candidate_validator_rejects_published_status():
    bundle = load_candidate_bundle()
    bundle["candidate_status"] = "released"

    with pytest.raises(CandidateGenerationError, match="non-published"):
        validate_candidate_bundle(bundle)
