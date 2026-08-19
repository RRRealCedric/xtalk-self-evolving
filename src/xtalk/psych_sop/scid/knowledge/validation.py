"""Deterministic validation for the phase-one SCID knowledge representation."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any, Iterable, Mapping

from .schema import (
    TERMINAL_NODE_ID,
    VALID_NODE_TYPES,
    VALID_SCORES,
    InterviewNode,
    KnowledgeBundle,
    Transition,
)


_NODE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
_ALLOWED_CONDITION_KEYS = frozenset(
    {"always", "score_in", "slot_equals", "all", "any", "not"}
)
_ALLOWED_EFFECT_TYPES = frozenset(
    {
        "queue_module",
        "record_pilot_outcome",
        "return_to_caller",
    }
)


class KnowledgeValidationError(ValueError):
    """Raised when a knowledge bundle cannot safely be compiled or used."""


def validate_knowledge_bundle(bundle: KnowledgeBundle) -> None:
    """Validate source traceability, semantic contracts, and graph safety."""

    if not bundle.bundle_id.strip():
        raise KnowledgeValidationError("Knowledge bundle id must not be empty")
    if not bundle.nodes:
        raise KnowledgeValidationError("Knowledge bundle must contain nodes")
    if not bundle.entry_node_ids:
        raise KnowledgeValidationError("Knowledge bundle must contain entry nodes")

    for entry_node_id in bundle.entry_node_ids:
        if entry_node_id not in bundle.nodes:
            raise KnowledgeValidationError(
                f"Entry node does not exist: {entry_node_id}"
            )

    for node in bundle.nodes.values():
        _validate_node(node, all_nodes=bundle.nodes)

    reachable = _reachable_nodes(bundle.entry_node_ids, bundle.nodes)
    unreachable = sorted(set(bundle.nodes) - reachable)
    if unreachable:
        raise KnowledgeValidationError(
            f"Knowledge bundle contains unreachable nodes: {unreachable}"
        )

    terminal_reachable = _nodes_that_can_reach_terminal(bundle.nodes)
    dead_ends = sorted(set(reachable) - terminal_reachable)
    if dead_ends:
        raise KnowledgeValidationError(
            "Reachable nodes cannot reach a terminal outcome: " f"{dead_ends}"
        )


def transition_accepts_score(transition: Transition, score: str) -> bool:
    """Return whether a simple phase-one transition accepts a given score."""

    if score not in VALID_SCORES:
        raise KnowledgeValidationError(f"Unsupported SCID score: {score!r}")
    when = transition.when
    if when.get("always") is True:
        return True
    values = when.get("score_in")
    return isinstance(values, (list, tuple)) and score in values


def _validate_node(
    node: InterviewNode, *, all_nodes: Mapping[str, InterviewNode]
) -> None:
    if not _NODE_ID_RE.fullmatch(node.node_id):
        raise KnowledgeValidationError(f"Invalid node id: {node.node_id!r}")
    if node.node_type not in VALID_NODE_TYPES:
        raise KnowledgeValidationError(
            f"Node {node.node_id} has invalid node type: {node.node_type!r}"
        )
    if not node.module_id.strip() or not node.clinical_intent.strip():
        raise KnowledgeValidationError(
            f"Node {node.node_id} must have module_id and clinical_intent"
        )
    if not node.source_refs:
        raise KnowledgeValidationError(f"Node {node.node_id} has no source provenance")
    if not node.transitions:
        raise KnowledgeValidationError(f"Node {node.node_id} has no transitions")
    if not node.dialogue_contract.core_concept.strip():
        raise KnowledgeValidationError(
            f"Node {node.node_id} dialogue contract lacks core_concept"
        )

    slot_ids = [item.slot_id for item in node.evidence_slots]
    if len(slot_ids) != len(set(slot_ids)):
        raise KnowledgeValidationError(
            f"Node {node.node_id} contains duplicate evidence slots"
        )
    if any(not _NODE_ID_RE.fullmatch(item) for item in slot_ids):
        raise KnowledgeValidationError(
            f"Node {node.node_id} contains invalid evidence slot id"
        )
    for slot in node.evidence_slots:
        invalid_scores = set(slot.required_for_scores) - VALID_SCORES
        if invalid_scores:
            raise KnowledgeValidationError(
                f"Node {node.node_id} evidence slot {slot.slot_id!r} has "
                f"invalid score requirements: {sorted(invalid_scores)}"
            )
    required_information = set(node.dialogue_contract.required_information)
    unknown_required = sorted(required_information - set(slot_ids))
    if unknown_required:
        raise KnowledgeValidationError(
            f"Node {node.node_id} dialogue contract references unknown slots: "
            f"{unknown_required}"
        )

    scores = [item.score for item in node.score_requirements]
    if len(scores) != len(set(scores)):
        raise KnowledgeValidationError(
            f"Node {node.node_id} contains duplicate score requirements"
        )
    for requirement in node.score_requirements:
        if requirement.score not in VALID_SCORES:
            raise KnowledgeValidationError(
                f"Node {node.node_id} has invalid score: {requirement.score}"
            )
        missing = sorted(set(requirement.required_slots) - set(slot_ids))
        if missing:
            raise KnowledgeValidationError(
                f"Node {node.node_id} score {requirement.score} references "
                f"unknown slots: {missing}"
            )

    levels = [item.level for item in node.dialogue_contract.clarification_ladder]
    if levels != sorted(levels) or len(levels) != len(set(levels)):
        raise KnowledgeValidationError(
            f"Node {node.node_id} clarification levels must be unique and ordered"
        )
    for step in node.dialogue_contract.clarification_ladder:
        missing = sorted(set(step.when_missing) - set(slot_ids))
        if missing:
            raise KnowledgeValidationError(
                f"Node {node.node_id} clarification step {step.level} references "
                f"unknown slots: {missing}"
            )

    defaults = 0
    transition_ids: set[str] = set()
    for transition in node.transitions:
        if transition.transition_id in transition_ids:
            raise KnowledgeValidationError(
                f"Node {node.node_id} contains duplicate transition id "
                f"{transition.transition_id!r}"
            )
        transition_ids.add(transition.transition_id)
        _validate_transition(node, transition, all_nodes=all_nodes)
        defaults += int(transition.when.get("always") is True)
    if defaults != 1:
        raise KnowledgeValidationError(
            f"Node {node.node_id} must have exactly one default transition"
        )


def _validate_transition(
    node: InterviewNode,
    transition: Transition,
    *,
    all_nodes: Mapping[str, InterviewNode],
) -> None:
    if not transition.transition_id.strip():
        raise KnowledgeValidationError(
            f"Node {node.node_id} has an empty transition id"
        )
    if transition.target_node_id != TERMINAL_NODE_ID and (
        transition.target_node_id not in all_nodes
    ):
        raise KnowledgeValidationError(
            f"Transition {transition.transition_id} targets missing node "
            f"{transition.target_node_id!r}"
        )
    _validate_condition(node, transition.when)
    for effect in transition.effects:
        if not isinstance(effect, Mapping):
            raise KnowledgeValidationError(
                f"Transition {transition.transition_id} contains a non-object effect"
            )
        effect_type = effect.get("type")
        if effect_type not in _ALLOWED_EFFECT_TYPES:
            raise KnowledgeValidationError(
                f"Transition {transition.transition_id} has invalid effect "
                f"{effect_type!r}"
            )


def _validate_condition(node: InterviewNode, condition: Mapping[str, Any]) -> None:
    if not isinstance(condition, Mapping) or not condition:
        raise KnowledgeValidationError(
            f"Node {node.node_id} transition condition must be a non-empty object"
        )
    unknown = set(condition) - _ALLOWED_CONDITION_KEYS
    if unknown:
        raise KnowledgeValidationError(
            f"Node {node.node_id} transition has unsupported conditions: "
            f"{sorted(unknown)}"
        )
    if condition.get("always") is True and len(condition) != 1:
        raise KnowledgeValidationError(
            f"Node {node.node_id} default transition cannot add other conditions"
        )
    if "score_in" in condition:
        values = condition["score_in"]
        if not isinstance(values, (list, tuple)) or not values:
            raise KnowledgeValidationError(
                f"Node {node.node_id} score_in must be a non-empty list"
            )
        invalid_scores = set(values) - VALID_SCORES
        if invalid_scores:
            raise KnowledgeValidationError(
                f"Node {node.node_id} has invalid score condition: "
                f"{sorted(invalid_scores)}"
            )


def _reachable_nodes(
    roots: Iterable[str], nodes: Mapping[str, InterviewNode]
) -> set[str]:
    seen: set[str] = set()
    pending = deque(roots)
    while pending:
        node_id = pending.popleft()
        if node_id in seen:
            continue
        seen.add(node_id)
        for transition in nodes[node_id].transitions:
            if transition.target_node_id != TERMINAL_NODE_ID:
                pending.append(transition.target_node_id)
    return seen


def _nodes_that_can_reach_terminal(nodes: Mapping[str, InterviewNode]) -> set[str]:
    reverse_edges: dict[str, set[str]] = defaultdict(set)
    terminal_predecessors: set[str] = set()
    for node in nodes.values():
        for transition in node.transitions:
            if transition.target_node_id == TERMINAL_NODE_ID:
                terminal_predecessors.add(node.node_id)
            else:
                reverse_edges[transition.target_node_id].add(node.node_id)

    result = set(terminal_predecessors)
    pending = deque(terminal_predecessors)
    while pending:
        node_id = pending.popleft()
        for predecessor in reverse_edges[node_id]:
            if predecessor not in result:
                result.add(predecessor)
                pending.append(predecessor)
    return result
