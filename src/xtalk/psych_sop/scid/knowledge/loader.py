"""Load phase-one SCID knowledge JSON without granting runtime write authority."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .schema import (
    ClarificationStep,
    DialogueContract,
    EvidenceSlot,
    InterviewNode,
    KnowledgeBundle,
    ScoreRequirement,
    SourceRef,
    Transition,
)
from .validation import (
    KnowledgeValidationError,
    transition_accepts_score,
    validate_knowledge_bundle,
)


PACKAGE_SCID_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEFAULT_SCID_KNOWLEDGE_DIR = PACKAGE_SCID_DATA_DIR / "scid_knowledge"
_MAX_JSON_BYTES = 2 * 1024 * 1024


def load_knowledge_bundle(root: str | Path | None = None) -> KnowledgeBundle:
    """Load and validate the modular phase-one SCID knowledge bundle."""

    root_path = Path(root) if root is not None else DEFAULT_SCID_KNOWLEDGE_DIR
    manifest = _load_json(root_path / "manifest.json")
    modules = manifest.get("modules")
    if not isinstance(modules, list) or not modules:
        raise KnowledgeValidationError(
            "Knowledge manifest must define non-empty modules"
        )

    nodes: dict[str, InterviewNode] = {}
    for module in modules:
        if not isinstance(module, Mapping):
            raise KnowledgeValidationError(
                "Knowledge manifest module must be an object"
            )
        relative_path = _required_string(module.get("path"), "module.path")
        payload = _load_json(root_path / relative_path)
        for raw_node in _required_list(payload.get("nodes"), "module.nodes"):
            node = _parse_node(raw_node)
            if node.node_id in nodes:
                raise KnowledgeValidationError(
                    f"Duplicate knowledge node id: {node.node_id}"
                )
            nodes[node.node_id] = node

    bundle = KnowledgeBundle(
        bundle_id=_required_string(manifest.get("bundle_id"), "bundle_id"),
        schema_version=_required_string(
            manifest.get("schema_version"), "schema_version"
        ),
        content_version=_required_string(
            manifest.get("content_version"), "content_version"
        ),
        language=_required_string(manifest.get("language"), "language"),
        source_document=_required_mapping(
            manifest.get("source_document"), "source_document"
        ),
        entry_node_ids=tuple(
            _required_string(item, "entry_node_ids[]")
            for item in _required_list(manifest.get("entry_node_ids"), "entry_node_ids")
        ),
        nodes=nodes,
        content_review_status=_required_string(
            manifest.get("content_review_status"), "content_review_status"
        ),
        deployment_scope=_required_string(
            manifest.get("deployment_scope"), "deployment_scope"
        ),
        metadata=_optional_mapping(manifest.get("metadata")),
    )
    validate_knowledge_bundle(bundle)
    return bundle


def load_trajectory_fixtures(
    root: str | Path | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Load bounded, synthetic-only phase-one trajectory fixtures."""

    root_path = Path(root) if root is not None else DEFAULT_SCID_KNOWLEDGE_DIR
    payload = _load_json(root_path / "fixtures" / "g_pilot_trajectories.json")
    cases = _required_list(payload.get("cases"), "fixtures.cases")
    return tuple(_required_mapping(item, "fixtures.case") for item in cases)


def validate_trajectory_fixtures(
    bundle: KnowledgeBundle,
    cases: tuple[Mapping[str, Any], ...],
) -> None:
    """Ensure synthetic trajectories refer to legal nodes and edges only."""

    case_ids: set[str] = set()
    for case in cases:
        case_id = _required_string(case.get("case_id"), "fixture.case_id")
        if case_id in case_ids:
            raise KnowledgeValidationError(f"Duplicate fixture case id: {case_id}")
        case_ids.add(case_id)
        if case.get("data_class") != "synthetic":
            raise KnowledgeValidationError(f"Fixture {case_id} is not marked synthetic")
        steps = _required_list(case.get("steps"), f"fixture {case_id}.steps")
        if not steps:
            raise KnowledgeValidationError(f"Fixture {case_id} has no steps")
        for index, raw_step in enumerate(steps):
            step = _required_mapping(raw_step, f"fixture {case_id}.step")
            node_id = _required_string(step.get("node_id"), "fixture.node_id")
            node = bundle.get_node(node_id)
            raw_score = step.get("score")
            score = (
                _required_string(raw_score, "fixture.score")
                if raw_score is not None
                else None
            )
            transition_id = _required_string(
                step.get("transition_id"), "fixture.transition_id"
            )
            transition = next(
                (
                    item
                    for item in node.transitions
                    if item.transition_id == transition_id
                ),
                None,
            )
            if transition is None:
                raise KnowledgeValidationError(
                    f"Fixture {case_id} references unknown transition "
                    f"{transition_id!r}"
                )
            if score is None and transition.when.get("always") is not True:
                raise KnowledgeValidationError(
                    f"Fixture {case_id} omits a score for a non-default "
                    f"transition {transition_id!r}"
                )
            if score is not None and not transition_accepts_score(transition, score):
                raise KnowledgeValidationError(
                    f"Fixture {case_id} score {score!r} does not match "
                    f"transition {transition_id!r}"
                )
            if index + 1 < len(steps):
                next_step = _required_mapping(
                    steps[index + 1], f"fixture {case_id}.next_step"
                )
                expected_target = _required_string(
                    next_step.get("node_id"), "fixture.next_node_id"
                )
                if transition.target_node_id != expected_target:
                    raise KnowledgeValidationError(
                        f"Fixture {case_id} transition {transition_id!r} targets "
                        f"{transition.target_node_id!r}, not {expected_target!r}"
                    )
            elif transition.target_node_id != "$terminal":
                raise KnowledgeValidationError(
                    f"Fixture {case_id} does not end at a terminal transition"
                )


def _parse_node(raw: Any) -> InterviewNode:
    payload = _required_mapping(raw, "node")
    evidence_slots = tuple(
        EvidenceSlot(
            slot_id=_required_string(item.get("slot_id"), "evidence_slot.slot_id"),
            value_type=_required_string(
                item.get("value_type"), "evidence_slot.value_type"
            ),
            description=_required_string(
                item.get("description"), "evidence_slot.description"
            ),
            required_for_scores=tuple(
                _required_string(score, "evidence_slot.required_for_scores[]")
                for score in _optional_list(item.get("required_for_scores"))
            ),
        )
        for item in _required_list(payload.get("evidence_slots"), "node.evidence_slots")
        if isinstance(item, Mapping)
    )
    if len(evidence_slots) != len(
        _required_list(payload.get("evidence_slots"), "node.evidence_slots")
    ):
        raise KnowledgeValidationError("Each evidence slot must be an object")

    dialogue = _required_mapping(
        payload.get("dialogue_contract"), "node.dialogue_contract"
    )
    ladder = tuple(
        ClarificationStep(
            level=_required_int(item.get("level"), "clarification_step.level"),
            when_missing=tuple(
                _required_string(value, "clarification_step.when_missing[]")
                for value in _required_list(
                    item.get("when_missing"), "clarification_step.when_missing"
                )
            ),
            strategy=_required_string(
                item.get("strategy"), "clarification_step.strategy"
            ),
            prompt_intent=_required_string(
                item.get("prompt_intent"), "clarification_step.prompt_intent"
            ),
        )
        for item in _required_list(
            dialogue.get("clarification_ladder"), "clarification_ladder"
        )
        if isinstance(item, Mapping)
    )
    if len(ladder) != len(
        _required_list(dialogue.get("clarification_ladder"), "clarification_ladder")
    ):
        raise KnowledgeValidationError("Each clarification step must be an object")

    return InterviewNode(
        node_id=_required_string(payload.get("node_id"), "node.node_id"),
        module_id=_required_string(payload.get("module_id"), "node.module_id"),
        node_type=_required_string(payload.get("node_type"), "node.node_type"),
        clinical_intent=_required_string(
            payload.get("clinical_intent"), "node.clinical_intent"
        ),
        time_window=_required_string(payload.get("time_window"), "node.time_window"),
        canonical_prompt=str(payload.get("canonical_prompt") or "").strip(),
        source_refs=tuple(
            _parse_source_ref(item)
            for item in _required_list(payload.get("source_refs"), "node.source_refs")
        ),
        evidence_slots=evidence_slots,
        score_requirements=tuple(
            ScoreRequirement(
                score=_required_string(item.get("score"), "score_requirement.score"),
                required_slots=tuple(
                    _required_string(value, "score_requirement.required_slots[]")
                    for value in _optional_list(item.get("required_slots"))
                ),
                requires_assessor_judgment=bool(item.get("requires_assessor_judgment")),
                summary=_required_string(
                    item.get("summary"), "score_requirement.summary"
                ),
            )
            for item in _required_list(
                payload.get("score_requirements"), "node.score_requirements"
            )
            if isinstance(item, Mapping)
        ),
        dialogue_contract=DialogueContract(
            core_concept=_required_string(
                dialogue.get("core_concept"), "dialogue_contract.core_concept"
            ),
            time_window=_required_string(
                dialogue.get("time_window"), "dialogue_contract.time_window"
            ),
            severity_threshold=_required_string(
                dialogue.get("severity_threshold"),
                "dialogue_contract.severity_threshold",
            ),
            key_exclusions=tuple(
                _required_string(value, "dialogue_contract.key_exclusions[]")
                for value in _optional_list(dialogue.get("key_exclusions"))
            ),
            required_information=tuple(
                _required_string(value, "dialogue_contract.required_information[]")
                for value in _required_list(
                    dialogue.get("required_information"),
                    "dialogue_contract.required_information",
                )
            ),
            neutral_examples=tuple(
                _required_string(value, "dialogue_contract.neutral_examples[]")
                for value in _optional_list(dialogue.get("neutral_examples"))
            ),
            allowed_paraphrases=tuple(
                _required_string(value, "dialogue_contract.allowed_paraphrases[]")
                for value in _optional_list(dialogue.get("allowed_paraphrases"))
            ),
            clarification_ladder=ladder,
        ),
        transitions=tuple(
            _parse_transition(item)
            for item in _required_list(payload.get("transitions"), "node.transitions")
        ),
        review_status=_required_string(
            payload.get("review_status"), "node.review_status"
        ),
        safety_sensitive=bool(payload.get("safety_sensitive", False)),
        metadata=_optional_mapping(payload.get("metadata")),
    )


def _parse_source_ref(raw: Any) -> SourceRef:
    item = _required_mapping(raw, "source_ref")
    return SourceRef(
        source_id=_required_string(item.get("source_id"), "source_ref.source_id"),
        kind=_required_string(item.get("kind"), "source_ref.kind"),
        locator=_required_string(item.get("locator"), "source_ref.locator"),
        description=str(item.get("description") or "").strip(),
        pdf_page=_optional_positive_int(item.get("pdf_page")),
        printed_page=_optional_positive_int(item.get("printed_page")),
        field_id=_optional_string(item.get("field_id")),
    )


def _parse_transition(raw: Any) -> Transition:
    item = _required_mapping(raw, "transition")
    effects = _optional_list(item.get("effects"))
    if not all(isinstance(effect, Mapping) for effect in effects):
        raise KnowledgeValidationError("Each transition effect must be an object")
    return Transition(
        transition_id=_required_string(
            item.get("transition_id"), "transition.transition_id"
        ),
        when=_required_mapping(item.get("when"), "transition.when"),
        target_node_id=_required_string(
            item.get("target_node_id"), "transition.target_node_id"
        ),
        effects=tuple(effects),
    )


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise KnowledgeValidationError(
            f"Unable to inspect knowledge JSON: {path}"
        ) from exc
    if size > _MAX_JSON_BYTES:
        raise KnowledgeValidationError(f"Knowledge JSON is too large: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw, parse_constant=_invalid_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KnowledgeValidationError(
            f"Unable to parse knowledge JSON: {path}"
        ) from exc
    return _required_mapping(value, f"JSON object at {path}")


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _required_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise KnowledgeValidationError(f"{field_name} must be an object")
    return value


def _optional_mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    return _required_mapping(value, "optional mapping")


def _required_list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise KnowledgeValidationError(f"{field_name} must be a list")
    return value


def _optional_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return _required_list(value, "optional list")


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _required_string(value, "optional string")


def _required_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise KnowledgeValidationError(f"{field_name} must be a positive integer")
    return value


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    return _required_int(value, "optional positive integer")
