from dataclasses import replace

import pytest

from xtalk.psych_sop.scid.knowledge import (
    KnowledgeValidationError,
    load_knowledge_bundle,
    load_trajectory_fixtures,
    validate_knowledge_bundle,
    validate_trajectory_fixtures,
)


def test_phase_one_g_pilot_loads_with_source_traceability():
    bundle = load_knowledge_bundle()

    assert bundle.bundle_id == "scid5-zh-g-pilot"
    assert bundle.entry_node_ids == ("S9", "S12")
    assert bundle.content_review_status.endswith("pending_clinical_review")

    g3 = bundle.get_node("G3")
    assert g3.source_refs[0].pdf_page == 232
    assert {slot.slot_id for slot in g3.evidence_slots} == {
        "occurrence",
        "content_description",
        "recurrent_or_intrusive_quality",
    }
    assert [step.level for step in g3.dialogue_contract.clarification_ladder] == [
        1,
        2,
        3,
    ]


def test_phase_one_fixtures_are_synthetic_and_follow_legal_edges():
    bundle = load_knowledge_bundle()
    cases = load_trajectory_fixtures()

    validate_trajectory_fixtures(bundle, cases)
    assert len(cases) == 5
    assert all(case["data_class"] == "synthetic" for case in cases)


def test_validator_rejects_dangling_transition_target():
    bundle = load_knowledge_bundle()
    source = bundle.get_node("S9")
    broken_transition = replace(source.transitions[0], target_node_id="MISSING.NODE")
    broken_node = replace(
        source,
        transitions=(broken_transition, *source.transitions[1:]),
    )
    broken_bundle = replace(
        bundle,
        nodes={**bundle.nodes, source.node_id: broken_node},
    )

    with pytest.raises(KnowledgeValidationError, match="targets missing node"):
        validate_knowledge_bundle(broken_bundle)


def test_validator_rejects_non_synthetic_fixture_data():
    bundle = load_knowledge_bundle()
    case = dict(load_trajectory_fixtures()[0])
    case["data_class"] = "real_patient"

    with pytest.raises(KnowledgeValidationError, match="not marked synthetic"):
        validate_trajectory_fixtures(bundle, (case,))
