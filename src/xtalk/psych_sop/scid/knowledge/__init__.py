"""Versioned, source-traceable SCID knowledge-bundle primitives.

The package is deliberately offline/content-facing.  It validates a compiled
interview definition but does not give any model permission to score, diagnose,
or mutate the runtime ledger.
"""

from .loader import (
    DEFAULT_SCID_KNOWLEDGE_DIR,
    load_knowledge_bundle,
    load_trajectory_fixtures,
    validate_trajectory_fixtures,
)
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
from .validation import KnowledgeValidationError, validate_knowledge_bundle
from .review_packet import build_review_packet
from .pdf_inventory import (
    DEFAULT_INVENTORY_DIR,
    EXPECTED_ACROFORM_FIELD_COUNT,
    PDFInventoryError,
    build_pdf_inventory,
    load_pdf_inventory,
    validate_pdf_inventory,
)
from .ocr_layout import (
    DEFAULT_SOURCE_MAP_DIR,
    OCRLayoutError,
    build_ocr_layout,
    load_ocr_layout,
    validate_ocr_layout,
)
from .candidate_generation import (
    CANDIDATE_STATUS,
    DEFAULT_CANDIDATES_DIR,
    CandidateGenerationError,
    build_g_pilot_candidates,
    load_candidate_bundle,
    validate_candidate_bundle,
)

__all__ = [
    "ClarificationStep",
    "DEFAULT_SCID_KNOWLEDGE_DIR",
    "DialogueContract",
    "EvidenceSlot",
    "InterviewNode",
    "KnowledgeBundle",
    "KnowledgeValidationError",
    "ScoreRequirement",
    "SourceRef",
    "Transition",
    "load_knowledge_bundle",
    "load_trajectory_fixtures",
    "validate_knowledge_bundle",
    "validate_trajectory_fixtures",
    "build_review_packet",
    "DEFAULT_INVENTORY_DIR",
    "EXPECTED_ACROFORM_FIELD_COUNT",
    "PDFInventoryError",
    "build_pdf_inventory",
    "load_pdf_inventory",
    "validate_pdf_inventory",
    "DEFAULT_SOURCE_MAP_DIR",
    "OCRLayoutError",
    "build_ocr_layout",
    "load_ocr_layout",
    "validate_ocr_layout",
    "CANDIDATE_STATUS",
    "DEFAULT_CANDIDATES_DIR",
    "CandidateGenerationError",
    "build_g_pilot_candidates",
    "load_candidate_bundle",
    "validate_candidate_bundle",
]
