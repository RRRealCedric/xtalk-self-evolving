"""Typed intermediate representation for executable SCID knowledge bundles.

These objects model source provenance, clinical information requirements, and
deterministic flow separately from user-facing rendering.  They intentionally
do not contain a diagnosis engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


VALID_NODE_TYPES = frozenset(
    {
        "screening_question",
        "criterion_question",
        "evidence_probe",
        "module_gate",
        "module_summary",
        "return",
    }
)
VALID_SCORES = frozenset({"?", "1", "2", "3"})
TERMINAL_NODE_ID = "$terminal"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A reviewable pointer to source or engineering provenance."""

    source_id: str
    kind: str
    locator: str
    description: str = ""
    pdf_page: int | None = None
    printed_page: int | None = None
    field_id: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceSlot:
    """One fact the interviewer needs before a score can be proposed."""

    slot_id: str
    value_type: str
    description: str
    required_for_scores: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClarificationStep:
    """A non-leading strategy for one missing or conflicted evidence slot."""

    level: int
    when_missing: tuple[str, ...]
    strategy: str
    prompt_intent: str


@dataclass(frozen=True, slots=True)
class DialogueContract:
    """Semantic constraints passed to a future dialogue planner."""

    core_concept: str
    time_window: str
    severity_threshold: str
    key_exclusions: tuple[str, ...]
    required_information: tuple[str, ...]
    neutral_examples: tuple[str, ...]
    allowed_paraphrases: tuple[str, ...]
    clarification_ladder: tuple[ClarificationStep, ...]


@dataclass(frozen=True, slots=True)
class ScoreRequirement:
    """Evidence sufficiency requirement, not an autonomous clinical diagnosis."""

    score: str
    required_slots: tuple[str, ...]
    requires_assessor_judgment: bool
    summary: str


@dataclass(frozen=True, slots=True)
class Transition:
    """One deterministic outgoing edge from an interview node."""

    transition_id: str
    when: Mapping[str, Any]
    target_node_id: str
    effects: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class InterviewNode:
    """One source-traceable node in a SCID interview graph."""

    node_id: str
    module_id: str
    node_type: str
    clinical_intent: str
    time_window: str
    canonical_prompt: str
    source_refs: tuple[SourceRef, ...]
    evidence_slots: tuple[EvidenceSlot, ...]
    score_requirements: tuple[ScoreRequirement, ...]
    dialogue_contract: DialogueContract
    transitions: tuple[Transition, ...]
    review_status: str
    safety_sensitive: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def score_requirement(self, score: str) -> ScoreRequirement | None:
        """Return the evidence requirement for a score when one exists."""

        return next(
            (item for item in self.score_requirements if item.score == score), None
        )


@dataclass(frozen=True, slots=True)
class KnowledgeBundle:
    """Read-only compiled knowledge definition for one content version."""

    bundle_id: str
    schema_version: str
    content_version: str
    language: str
    source_document: Mapping[str, Any]
    entry_node_ids: tuple[str, ...]
    nodes: Mapping[str, InterviewNode]
    content_review_status: str
    deployment_scope: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def get_node(self, node_id: str) -> InterviewNode:
        """Return an interview node or fail explicitly for an invalid graph edge."""

        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise KeyError(f"Unknown SCID knowledge node: {node_id}") from exc
