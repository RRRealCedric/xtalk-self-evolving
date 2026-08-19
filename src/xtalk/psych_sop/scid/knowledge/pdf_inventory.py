"""Deterministic, read-only AcroForm inventory generation for SCID source PDFs.

The inventory is structural provenance, not a clinical interpretation.  It
records form fields, widgets, and PDF actions so later content work can cite a
stable anchor without treating a field name or screen position as a diagnosis
criterion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


_PROJECT_ROOT = Path(__file__).resolve().parents[6]
DEFAULT_SCID_PDF_PATH = (
    _PROJECT_ROOT / "psydata" / "realdata" / "PsychologySOP-Template" / "scid-5.pdf"
)
DEFAULT_SCID_SOURCE_MANIFEST_PATH = (
    _PROJECT_ROOT
    / "psydata"
    / "realdata"
    / "PsychologySOP-Template"
    / "scid-source-manifest.json"
)
DEFAULT_SCID_KNOWLEDGE_DIR = (
    _PROJECT_ROOT / "xtalk" / "src" / "xtalk" / "psych_sop" / "data" / "scid_knowledge"
)
DEFAULT_INVENTORY_DIR = DEFAULT_SCID_KNOWLEDGE_DIR / "inventory"
DEFAULT_PILOT_PATH = DEFAULT_SCID_KNOWLEDGE_DIR / "modules" / "G" / "pilot.json"
DEFAULT_REVIEW_LAYOUT_PATH = (
    DEFAULT_SCID_KNOWLEDGE_DIR / "reviews" / "g-pilot-layout.json"
)

INVENTORY_VERSION = "1.0.0"
GENERATOR_VERSION = "1.0.0"
EXPECTED_ACROFORM_FIELD_COUNT = 1958
MAX_PDF_BYTES = 128 * 1024 * 1024
MAX_PDF_PAGES = 1000

_FIELD_TYPE_NAMES = {"/Btn": "button", "/Tx": "text", "/Ch": "choice"}
_RADIO_FLAG = 1 << 15
_PUSHBUTTON_FLAG = 1 << 16

# These relations are deliberately narrow.  They document that the static G3
# and G11 source regions are structurally adjacent to the corresponding scan
# form anchors, rather than silently claiming an exact AcroForm field exists.
_PILOT_RELATED_FORM_ANCHORS = {
    "G3": "S9-G3",
    "G11": "S12-G11",
}


class PDFInventoryError(ValueError):
    """Raised when a source PDF cannot produce a valid structural inventory."""


def build_pdf_inventory(
    *,
    pdf_path: str | Path = DEFAULT_SCID_PDF_PATH,
    output_dir: str | Path = DEFAULT_INVENTORY_DIR,
    source_manifest_path: str | Path = DEFAULT_SCID_SOURCE_MANIFEST_PATH,
    pilot_path: str | Path = DEFAULT_PILOT_PATH,
    review_layout_path: str | Path = DEFAULT_REVIEW_LAYOUT_PATH,
    expected_field_count: int = EXPECTED_ACROFORM_FIELD_COUNT,
) -> Path:
    """Extract and write the Phase 2 inventory artifacts.

    The resulting files intentionally contain no OCR output and no interview
    data.  Re-running against the same source bytes and parser version produces
    byte-identical JSON and Markdown artifacts.
    """

    payload = extract_pdf_inventory(
        pdf_path=pdf_path,
        source_manifest_path=source_manifest_path,
        expected_field_count=expected_field_count,
    )
    validate_pdf_inventory(payload, expected_field_count=expected_field_count)
    pilot_locations = build_pilot_locations(
        payload,
        pilot_path=pilot_path,
        review_layout_path=review_layout_path,
    )
    anomalies = build_anomaly_report(payload, pilot_locations)

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    _write_json(destination / "scid-5.acroform-inventory.json", payload)
    _write_json(destination / "scid-5.pilot-locations.json", pilot_locations)
    _write_json(destination / "scid-5.anomalies.json", anomalies)
    _write_text(
        destination / "scid-5.inventory-report.md",
        render_inventory_report(payload, anomalies),
    )
    return destination


def extract_pdf_inventory(
    *,
    pdf_path: str | Path = DEFAULT_SCID_PDF_PATH,
    source_manifest_path: str | Path = DEFAULT_SCID_SOURCE_MANIFEST_PATH,
    expected_field_count: int = EXPECTED_ACROFORM_FIELD_COUNT,
) -> dict[str, Any]:
    """Return a JSON-serializable, field-centric AcroForm inventory."""

    PdfReader, pypdf_version = _load_pypdf()
    source_path = Path(pdf_path)
    _validate_source_path(source_path)
    source_sha256 = _sha256(source_path)
    source_manifest = _load_json_object(Path(source_manifest_path))
    source_document = source_manifest.get("source_document", {})
    expected_sha256 = source_document.get("sha256")
    if expected_sha256 and expected_sha256 != source_sha256:
        raise PDFInventoryError(
            "SCID PDF SHA-256 does not match the frozen source manifest: "
            f"expected {expected_sha256}, got {source_sha256}"
        )

    try:
        reader = PdfReader(str(source_path))
        pages = list(reader.pages)
    except Exception as exc:  # pypdf has several parser-specific error types.
        raise PDFInventoryError(
            f"Unable to parse SCID source PDF: {source_path}"
        ) from exc
    if len(pages) > MAX_PDF_PAGES:
        raise PDFInventoryError(f"SCID PDF contains more than {MAX_PDF_PAGES} pages")

    try:
        raw_fields = reader.get_fields() or {}
    except Exception as exc:
        raise PDFInventoryError("Unable to enumerate AcroForm fields") from exc
    if not raw_fields:
        raise PDFInventoryError("SCID PDF has no AcroForm fields")
    if len(raw_fields) != expected_field_count:
        raise PDFInventoryError(
            "SCID AcroForm field count does not match the frozen source expectation: "
            f"expected {expected_field_count}, got {len(raw_fields)}"
        )

    fields, names_by_object_ref = _initial_field_records(raw_fields, source_document)
    page_by_object_ref = {
        _object_ref_key(getattr(page, "indirect_reference", None)): index
        for index, page in enumerate(pages, start=1)
        if _object_ref_key(getattr(page, "indirect_reference", None)) is not None
    }
    (
        field_widgets,
        unassociated_widgets,
        navigation_annotations,
        action_parse_errors,
    ) = _scan_page_widgets(
        pages,
        names_by_object_ref=names_by_object_ref,
        page_by_object_ref=page_by_object_ref,
    )

    for canonical_name, widgets in field_widgets.items():
        record = fields[canonical_name]
        record["widgets"] = sorted(
            widgets,
            key=lambda item: (
                item["page_number"],
                item["object_ref"] or "",
                item["rect_pdf_points"],
            ),
        )
        record["widget_count"] = len(record["widgets"])
        record["page_numbers"] = sorted(
            {item["page_number"] for item in record["widgets"]}
        )
        record["candidate_class"] = _candidate_class(record)

    for record in fields.values():
        record["widgets"] = record.get("widgets", [])
        record["widget_count"] = len(record["widgets"])
        record["page_numbers"] = sorted(
            {item["page_number"] for item in record["widgets"]}
        )
        record["candidate_class"] = _candidate_class(record)

    field_records = sorted(fields.values(), key=lambda item: item["canonical_name"])
    action_summary = _action_summary(
        field_records, unassociated_widgets, navigation_annotations
    )
    payload: dict[str, Any] = {
        "inventory_version": INVENTORY_VERSION,
        "generator": {
            "name": "xtalk.scid.pdf_inventory",
            "version": GENERATOR_VERSION,
            "parser": "pypdf",
            "parser_version": pypdf_version,
        },
        "source_document": {
            "document_id": source_document.get("document_id", "unknown"),
            "relative_path": _project_relative_path(source_path),
            "sha256": source_sha256,
            "size_bytes": source_path.stat().st_size,
            "page_count": len(pages),
            "coordinate_system": "PDF user space points; origin is the page lower-left before viewer rotation",
        },
        "coverage": {
            "expected_acroform_field_count": expected_field_count,
            "inventoried_field_count": len(field_records),
            "field_coverage_ratio": len(field_records) / expected_field_count,
            "widget_count": sum(item["widget_count"] for item in field_records),
            "unassociated_widget_count": len(unassociated_widgets),
            "navigation_annotation_count": len(navigation_annotations),
            "field_type_counts": dict(
                sorted(Counter(item["field_type"] for item in field_records).items())
            ),
        },
        "action_summary": action_summary,
        "fields": field_records,
        "unassociated_widgets": sorted(
            unassociated_widgets,
            key=lambda item: (item["page_number"], item["object_ref"] or ""),
        ),
        "navigation_annotations": sorted(
            navigation_annotations,
            key=lambda item: (item["page_number"], item["object_ref"] or ""),
        ),
        "parse_errors": action_parse_errors,
        "limitations": [
            "This inventory records PDF structure only; field names, actions, and locations are not clinical interpretations.",
            "Static question text and printed instructions require separate source-page/OCR review in later phases.",
            "Candidate classes are form-control classes, not SCID node types or diagnostic labels.",
        ],
    }
    return payload


def validate_pdf_inventory(
    payload: Mapping[str, Any],
    *,
    expected_field_count: int = EXPECTED_ACROFORM_FIELD_COUNT,
) -> None:
    """Validate Phase 2 release invariants without re-reading the PDF."""

    if not isinstance(payload, Mapping):
        raise PDFInventoryError("Inventory payload must be an object")
    coverage = payload.get("coverage")
    fields = payload.get("fields")
    if not isinstance(coverage, Mapping) or not isinstance(fields, list):
        raise PDFInventoryError("Inventory requires coverage and fields")
    if coverage.get("expected_acroform_field_count") != expected_field_count:
        raise PDFInventoryError("Inventory expected field count is inconsistent")
    if coverage.get("inventoried_field_count") != expected_field_count:
        raise PDFInventoryError(
            "Inventory does not cover every expected AcroForm field"
        )
    if len(fields) != expected_field_count:
        raise PDFInventoryError("Inventory field list count is inconsistent")

    stable_ids: set[str] = set()
    canonical_names: set[str] = set()
    goto_errors: list[str] = []
    for index, field in enumerate(fields):
        if not isinstance(field, Mapping):
            raise PDFInventoryError(f"Inventory field {index} is not an object")
        stable_id = field.get("stable_id")
        canonical_name = field.get("canonical_name")
        if not isinstance(stable_id, str) or not stable_id:
            raise PDFInventoryError(f"Inventory field {index} has no stable_id")
        if not isinstance(canonical_name, str) or not canonical_name:
            raise PDFInventoryError(f"Inventory field {index} has no canonical_name")
        if stable_id in stable_ids or canonical_name in canonical_names:
            raise PDFInventoryError(
                "Inventory stable IDs and field names must be unique"
            )
        stable_ids.add(stable_id)
        canonical_names.add(canonical_name)
        for widget in field.get("widgets", []):
            if not isinstance(widget, Mapping) or not isinstance(
                widget.get("page_number"), int
            ):
                raise PDFInventoryError(f"Field {canonical_name} has an invalid widget")
            rect = widget.get("rect_pdf_points")
            if not isinstance(rect, list) or len(rect) != 4:
                raise PDFInventoryError(
                    f"Field {canonical_name} has an invalid widget rect"
                )
            for action in widget.get("actions", []):
                if (
                    action.get("action_type") == "GoTo"
                    and action.get("parse_status") != "resolved"
                ):
                    goto_errors.append(f"{canonical_name}:{action.get('object_ref')}")
    for annotation in payload.get("navigation_annotations", []):
        if not isinstance(annotation, Mapping):
            raise PDFInventoryError("Inventory has an invalid navigation annotation")
        for action in annotation.get("actions", []):
            if (
                action.get("action_type") == "GoTo"
                and action.get("parse_status") != "resolved"
            ):
                goto_errors.append(f"annotation:{action.get('object_ref')}")
    if goto_errors:
        raise PDFInventoryError(
            "GoTo action(s) are not resolved: " + ", ".join(sorted(goto_errors))
        )


def load_pdf_inventory(
    path: str | Path = DEFAULT_INVENTORY_DIR / "scid-5.acroform-inventory.json",
) -> dict[str, Any]:
    """Load a committed inventory artifact and validate its structural invariants."""

    payload = _load_json_object(Path(path))
    validate_pdf_inventory(payload)
    return payload


def build_pilot_locations(
    inventory: Mapping[str, Any],
    *,
    pilot_path: str | Path = DEFAULT_PILOT_PATH,
    review_layout_path: str | Path = DEFAULT_REVIEW_LAYOUT_PATH,
) -> dict[str, Any]:
    """Map Phase 1 Pilot source refs to Phase 2 structural anchors.

    Exact matches are kept distinct from reviewed source-region or related-field
    matches.  This prevents static question text from being misrepresented as a
    form field merely because an adjacent response control exists.
    """

    fields = inventory.get("fields")
    if not isinstance(fields, list):
        raise PDFInventoryError("Pilot locations require an inventory field list")
    by_name = {
        item["canonical_name"]: item
        for item in fields
        if isinstance(item, Mapping) and isinstance(item.get("canonical_name"), str)
    }
    pilot = _load_json_object(Path(pilot_path))
    layout = _load_json_object(Path(review_layout_path))
    layout_by_node = _layout_assets_by_node(layout)

    locations: list[dict[str, Any]] = []
    for node in pilot.get("nodes", []):
        node_id = node.get("node_id")
        if not isinstance(node_id, str):
            continue
        refs: list[dict[str, Any]] = []
        for source_ref in node.get("source_refs", []):
            if not isinstance(source_ref, Mapping):
                continue
            field_id = source_ref.get("field_id")
            if not isinstance(field_id, str) or not field_id:
                continue
            expected_page = source_ref.get("pdf_page")
            refs.append(
                _resolve_pilot_source_ref(
                    node_id=node_id,
                    field_id=field_id,
                    source_kind=str(source_ref.get("kind", "unknown")),
                    declared_pdf_page=(
                        expected_page if isinstance(expected_page, int) else None
                    ),
                    by_name=by_name,
                )
            )
        if not refs:
            status = "not_applicable"
        elif all(
            item["status"] in {"exact_field_match", "related_field_match"}
            for item in refs
        ):
            status = "resolved"
        else:
            status = "needs_review"
        locations.append(
            {
                "node_id": node_id,
                "status": status,
                "source_refs": refs,
                "review_layout_assets": layout_by_node.get(node_id, []),
            }
        )

    return {
        "location_version": "1.0.0",
        "source_inventory_sha256": inventory.get("source_document", {}).get("sha256"),
        "pilot_path": _project_relative_path(Path(pilot_path)),
        "locations": locations,
        "limitations": [
            "An exact AcroForm match identifies a form field, not the semantic extent of a printed question.",
            "related_field_match is an explicit structural relation and must not be upgraded to an exact source-field claim.",
            "not_applicable nodes are engineering-only nodes without a PDF source anchor.",
        ],
    }


def build_anomaly_report(
    inventory: Mapping[str, Any], pilot_locations: Mapping[str, Any]
) -> dict[str, Any]:
    """Produce a machine-readable exception report for Phase 2 review."""

    fields = inventory.get("fields", [])
    unassociated = inventory.get("unassociated_widgets", [])
    parse_errors = inventory.get("parse_errors", [])
    fields_without_widgets = [
        item["canonical_name"]
        for item in fields
        if isinstance(item, Mapping) and item.get("widget_count") == 0
    ]
    pilot_needing_review = [
        item["node_id"]
        for item in pilot_locations.get("locations", [])
        if isinstance(item, Mapping) and item.get("status") == "needs_review"
    ]
    return {
        "report_version": "1.0.0",
        "source_inventory_sha256": inventory.get("source_document", {}).get("sha256"),
        "summary": {
            "field_count": len(fields),
            "fields_without_widgets": len(fields_without_widgets),
            "unassociated_widgets": len(unassociated),
            "pdf_parse_errors": len(parse_errors),
            "pilot_nodes_needing_review": len(pilot_needing_review),
        },
        "items": {
            "fields_without_widgets": fields_without_widgets,
            "unassociated_widgets": unassociated,
            "pdf_parse_errors": parse_errors,
            "pilot_nodes_needing_review": pilot_needing_review,
        },
        "review_guidance": [
            "Unassociated widgets are retained as structural exceptions; do not drop them or infer a clinical meaning.",
            "A nonzero JavaScript or ResetForm action count is not an error when its action type is explicitly classified.",
            "Any future unresolved GoTo destination is a release-blocking inventory exception.",
        ],
    }


def render_inventory_report(
    inventory: Mapping[str, Any], anomalies: Mapping[str, Any]
) -> str:
    """Render a deterministic, human-readable Phase 2 summary."""

    source = inventory["source_document"]
    coverage = inventory["coverage"]
    actions = inventory["action_summary"]
    summary = anomalies["summary"]
    type_rows = "\n".join(
        f"| `{field_type}` | {count} |"
        for field_type, count in coverage["field_type_counts"].items()
    )
    return "\n".join(
        [
            "# SCID-5 AcroForm Inventory",
            "",
            "## Scope",
            "",
            "This is a deterministic structural inventory of the frozen blank PDF. It does not contain OCR output, interview transcripts, or clinical interpretations.",
            "",
            "## Source",
            "",
            f"- Document: `{source['document_id']}`",
            f"- Path: `{source['relative_path']}`",
            f"- SHA-256: `{source['sha256']}`",
            f"- Pages: {source['page_count']}",
            f"- Coordinate system: {source['coordinate_system']}",
            "",
            "## Coverage",
            "",
            f"- AcroForm fields: {coverage['inventoried_field_count']} / {coverage['expected_acroform_field_count']} ({coverage['field_coverage_ratio']:.0%})",
            f"- Associated widgets: {coverage['widget_count']}",
            f"- Unassociated widgets: {coverage['unassociated_widget_count']}",
            f"- Non-widget navigation annotations: {coverage['navigation_annotation_count']}",
            "",
            "| Field type | Count |",
            "| --- | ---: |",
            type_rows,
            "",
            "## Actions",
            "",
            f"- GoTo: {actions['goto_resolved']} resolved, {actions['goto_errors']} errors",
            f"- Explicit non-GoTo actions: {actions['non_goto_actions']}",
            f"- Unrecognized actions: {actions['unrecognized_actions']}",
            "",
            "## Exceptions",
            "",
            f"- Fields without widgets: {summary['fields_without_widgets']}",
            f"- Unassociated widgets: {summary['unassociated_widgets']}",
            f"- PDF parse errors: {summary['pdf_parse_errors']}",
            f"- Pilot nodes needing source-location review: {summary['pilot_nodes_needing_review']}",
            "",
            "## Interpretation Boundary",
            "",
            "Field names, page locations, and GoTo destinations are only source anchors. They must not be treated as clinical concepts, score semantics, or diagnostic rules without later content review.",
            "",
        ]
    )


def _initial_field_records(
    raw_fields: Mapping[Any, Any], source_document: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    fields: dict[str, dict[str, Any]] = {}
    names_by_object_ref: dict[str, str] = {}
    source_id = str(source_document.get("document_id", "scid5-unknown-source"))
    for raw_name, field in raw_fields.items():
        canonical_name = str(raw_name)
        if not canonical_name or canonical_name in fields:
            raise PDFInventoryError(
                f"Invalid or duplicate AcroForm field name: {canonical_name!r}"
            )
        object_ref = _object_ref_key(getattr(field, "indirect_reference", None))
        if object_ref is None:
            raise PDFInventoryError(
                f"AcroForm field has no indirect object reference: {canonical_name}"
            )
        if object_ref in names_by_object_ref:
            raise PDFInventoryError(
                f"Duplicate AcroForm field object reference: {object_ref}"
            )
        field_flags = _safe_int(_as_object(field.get("/Ff")), default=0)
        raw_type = str(_as_object(field.get("/FT")) or "")
        fields[canonical_name] = {
            "stable_id": _stable_field_id(source_id, canonical_name),
            "canonical_name": canonical_name,
            "object_ref": object_ref,
            "field_type": _FIELD_TYPE_NAMES.get(raw_type, "unknown"),
            "field_flags": field_flags,
            "alternate_name": _optional_string(_as_object(field.get("/TU"))),
            "options": _json_value(_as_object(field.get("/Opt"))),
            "widgets": [],
            "widget_count": 0,
            "page_numbers": [],
            "candidate_class": "unclassified",
        }
        names_by_object_ref[object_ref] = canonical_name
    return fields, names_by_object_ref


def _scan_page_widgets(
    pages: list[Any],
    *,
    names_by_object_ref: Mapping[str, str],
    page_by_object_ref: Mapping[str, int],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    widgets_by_field: dict[str, list[dict[str, Any]]] = {
        name: [] for name in names_by_object_ref.values()
    }
    unassociated: list[dict[str, Any]] = []
    navigation_annotations: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    for page_number, page in enumerate(pages, start=1):
        annotations = _as_object(page.get("/Annots")) or []
        if not isinstance(annotations, (list, tuple)):
            parse_errors.append(
                {
                    "kind": "invalid_annotations_array",
                    "page_number": page_number,
                    "message": "Page /Annots is not an array",
                }
            )
            continue
        for widget_ref in annotations:
            widget = _as_object(widget_ref)
            if not isinstance(widget, Mapping):
                continue
            object_ref = _object_ref_key(widget_ref) or _object_ref_key(
                getattr(widget, "indirect_reference", None)
            )
            subtype = str(widget.get("/Subtype") or "unknown").lstrip("/")
            if subtype != "Widget":
                actions, errors = _extract_widget_actions(
                    widget,
                    page_by_object_ref=page_by_object_ref,
                    object_ref=object_ref,
                )
                parse_errors.extend(
                    {
                        "kind": "annotation_action_parse_error",
                        "page_number": page_number,
                        "object_ref": object_ref,
                        **error,
                    }
                    for error in errors
                )
                if actions:
                    navigation_annotations.append(
                        {
                            "object_ref": object_ref,
                            "annotation_subtype": subtype,
                            "page_number": page_number,
                            "rect_pdf_points": _rect(_as_object(widget.get("/Rect"))),
                            "page_rotation_degrees": _safe_int(
                                _as_object(page.get("/Rotate")), default=0
                            ),
                            "actions": actions,
                        }
                    )
                continue
            field_name = _resolve_widget_field_name(
                widget_ref, widget, names_by_object_ref=names_by_object_ref
            )
            actions, errors = _extract_widget_actions(
                widget,
                page_by_object_ref=page_by_object_ref,
                object_ref=object_ref,
            )
            parse_errors.extend(
                {
                    "kind": "action_parse_error",
                    "page_number": page_number,
                    "object_ref": object_ref,
                    **error,
                }
                for error in errors
            )
            record = {
                "object_ref": object_ref,
                "page_number": page_number,
                "rect_pdf_points": _rect(_as_object(widget.get("/Rect"))),
                "page_rotation_degrees": _safe_int(
                    _as_object(page.get("/Rotate")), default=0
                ),
                "actions": actions,
            }
            if field_name is None:
                record["reason"] = (
                    "Widget is not associated with an inventoried AcroForm field"
                )
                unassociated.append(record)
            else:
                widgets_by_field[field_name].append(record)
    return widgets_by_field, unassociated, navigation_annotations, parse_errors


def _resolve_widget_field_name(
    widget_ref: Any,
    widget: Mapping[str, Any],
    *,
    names_by_object_ref: Mapping[str, str],
) -> str | None:
    current_ref = widget_ref
    current: Any = widget
    visited: set[str] = set()
    while isinstance(current, Mapping):
        object_ref = _object_ref_key(current_ref) or _object_ref_key(
            getattr(current, "indirect_reference", None)
        )
        if object_ref is not None:
            if object_ref in visited:
                return None
            visited.add(object_ref)
            if object_ref in names_by_object_ref:
                return names_by_object_ref[object_ref]
        parent_ref = current.get("/Parent")
        if parent_ref is None:
            break
        current_ref = parent_ref
        current = _as_object(parent_ref)
    return None


def _extract_widget_actions(
    widget: Mapping[str, Any],
    *,
    page_by_object_ref: Mapping[str, int],
    object_ref: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    activate = widget.get("/A")
    if activate is not None:
        action, error = _parse_action(
            _as_object(activate),
            trigger="activate",
            page_by_object_ref=page_by_object_ref,
            object_ref=object_ref,
        )
        actions.append(action)
        if error:
            errors.append(error)
    additional = _as_object(widget.get("/AA"))
    if isinstance(additional, Mapping):
        for event, raw_action in sorted(
            additional.items(), key=lambda item: str(item[0])
        ):
            action, error = _parse_action(
                _as_object(raw_action),
                trigger=f"additional:{str(event).lstrip('/')}",
                page_by_object_ref=page_by_object_ref,
                object_ref=object_ref,
            )
            actions.append(action)
            if error:
                errors.append(error)
    return actions, errors


def _parse_action(
    action: Any,
    *,
    trigger: str,
    page_by_object_ref: Mapping[str, int],
    object_ref: str | None,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    if not isinstance(action, Mapping):
        return (
            {
                "object_ref": object_ref,
                "trigger": trigger,
                "action_type": "unknown",
                "parse_status": "parse_error",
                "message": "Action is not a dictionary",
            },
            {"message": "Action is not a dictionary"},
        )
    action_type = str(_as_object(action.get("/S")) or "unknown").lstrip("/")
    base = {
        "object_ref": object_ref,
        "trigger": trigger,
        "action_type": action_type,
    }
    if action_type == "GoTo":
        destination, message = _resolve_goto_destination(
            _as_object(action.get("/D")), page_by_object_ref=page_by_object_ref
        )
        if message is None:
            return (
                {**base, "parse_status": "resolved", "destination": destination},
                None,
            )
        return (
            {**base, "parse_status": "parse_error", "message": message},
            {"message": message},
        )
    if action_type == "ResetForm":
        return (
            {
                **base,
                "parse_status": "recognized_non_goto",
                "form_flags": _safe_int(_as_object(action.get("/Flags")), default=0),
                "field_count": len(_as_object(action.get("/Fields")) or []),
            },
            None,
        )
    if action_type == "JavaScript":
        return (
            {
                **base,
                "parse_status": "recognized_non_goto",
                "has_script_payload": action.get("/JS") is not None,
            },
            None,
        )
    return (
        {**base, "parse_status": "unrecognized_action_type"},
        None,
    )


def _resolve_goto_destination(
    destination: Any, *, page_by_object_ref: Mapping[str, int]
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(destination, (list, tuple)) or not destination:
        return None, "GoTo action has no destination array"
    page_ref = _object_ref_key(destination[0])
    page_number = page_by_object_ref.get(page_ref or "")
    if page_number is None:
        return None, f"GoTo destination page is not in this PDF: {page_ref!r}"
    mode = str(_as_object(destination[1])).lstrip("/") if len(destination) > 1 else None
    return (
        {
            "page_number": page_number,
            "destination_mode": mode,
            "arguments": [_json_value(_as_object(item)) for item in destination[2:]],
        },
        None,
    )


def _candidate_class(field: Mapping[str, Any]) -> str:
    actions = [
        action
        for widget in field.get("widgets", [])
        if isinstance(widget, Mapping)
        for action in widget.get("actions", [])
        if isinstance(action, Mapping)
    ]
    if any(action.get("action_type") == "GoTo" for action in actions):
        return "navigation_control"
    if actions:
        return "action_control"
    field_type = field.get("field_type")
    flags = _safe_int(field.get("field_flags"), default=0)
    if field_type == "text":
        return "text_input"
    if field_type == "choice":
        return "choice_input"
    if field_type == "button" and flags & _RADIO_FLAG:
        return "radio_group"
    if field_type == "button" and flags & _PUSHBUTTON_FLAG:
        return "push_button"
    if field_type == "button":
        return "toggle_or_checkbox"
    return "unclassified"


def _action_summary(
    fields: list[Mapping[str, Any]],
    unassociated_widgets: list[Mapping[str, Any]],
    navigation_annotations: list[Mapping[str, Any]],
) -> dict[str, int]:
    actions = [
        action
        for record in [*fields, *unassociated_widgets, *navigation_annotations]
        if isinstance(record, Mapping)
        for widget in ([record] if "actions" in record else record.get("widgets", []))
        if isinstance(widget, Mapping)
        for action in widget.get("actions", [])
        if isinstance(action, Mapping)
    ]
    return {
        "total_actions": len(actions),
        "goto_resolved": sum(
            action.get("action_type") == "GoTo"
            and action.get("parse_status") == "resolved"
            for action in actions
        ),
        "goto_errors": sum(
            action.get("action_type") == "GoTo"
            and action.get("parse_status") != "resolved"
            for action in actions
        ),
        "non_goto_actions": sum(
            action.get("parse_status") == "recognized_non_goto" for action in actions
        ),
        "unrecognized_actions": sum(
            action.get("parse_status") == "unrecognized_action_type"
            for action in actions
        ),
    }


def _resolve_pilot_source_ref(
    *,
    node_id: str,
    field_id: str,
    source_kind: str,
    declared_pdf_page: int | None,
    by_name: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    matched = by_name.get(field_id)
    relation = None
    if matched is None and field_id in _PILOT_RELATED_FORM_ANCHORS:
        related_name = _PILOT_RELATED_FORM_ANCHORS[field_id]
        matched = by_name.get(related_name)
        relation = (
            f"Static source anchor {field_id} has no exact AcroForm field; "
            f"using explicit related scan-to-module anchor {related_name}."
        )
    if matched is None:
        return {
            "source_kind": source_kind,
            "requested_field_id": field_id,
            "declared_pdf_page": declared_pdf_page,
            "status": "missing_field_anchor",
        }
    page_numbers = matched.get("page_numbers", [])
    return {
        "source_kind": source_kind,
        "requested_field_id": field_id,
        "declared_pdf_page": declared_pdf_page,
        "status": "related_field_match" if relation else "exact_field_match",
        "matched_field_id": matched["canonical_name"],
        "matched_stable_id": matched["stable_id"],
        "matched_page_numbers": page_numbers,
        "declared_page_present": (
            declared_pdf_page in page_numbers if declared_pdf_page is not None else None
        ),
        "relation_note": relation,
        "node_id": node_id,
    }


def _layout_assets_by_node(
    layout: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for asset in layout.get("source_crops", []):
        if not isinstance(asset, Mapping):
            continue
        summary = {
            "asset_id": asset.get("asset_id"),
            "pdf_page": asset.get("pdf_page"),
            "printed_page": asset.get("printed_page"),
            "normalized_box": asset.get("normalized_box"),
        }
        for node_id in asset.get("node_ids", []):
            if isinstance(node_id, str):
                result.setdefault(node_id, []).append(summary)
    return result


def _load_pypdf() -> tuple[Any, str]:
    try:
        import pypdf
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "pypdf is required to generate a SCID AcroForm inventory. "
            "It is intentionally imported only by the offline generator."
        ) from exc
    return PdfReader, str(getattr(pypdf, "__version__", "unknown"))


def _validate_source_path(path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PDFInventoryError(f"Unable to inspect SCID PDF: {path}") from exc
    if size > MAX_PDF_BYTES:
        raise PDFInventoryError(f"SCID PDF exceeds {MAX_PDF_BYTES} bytes")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_field_id(source_document_id: str, canonical_name: str) -> str:
    name_hash = hashlib.sha256(canonical_name.encode("utf-8")).hexdigest()[:16]
    return f"{source_document_id}:acroform:{name_hash}"


def _object_ref_key(value: Any) -> str | None:
    if value is None:
        return None
    identifier = getattr(value, "idnum", None)
    generation = getattr(value, "generation", None)
    if isinstance(identifier, int) and isinstance(generation, int):
        return f"{identifier} {generation} R"
    return None


def _as_object(value: Any) -> Any:
    if value is None:
        return None
    getter = getattr(value, "get_object", None)
    if callable(getter):
        return getter()
    return value


def _rect(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(_as_object(item)) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(_as_object(item)) for key, item in value.items()}
    object_ref = _object_ref_key(value)
    return {"object_ref": object_ref} if object_ref else str(value)


def _project_relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(_PROJECT_ROOT))
    except ValueError:
        return str(path)


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PDFInventoryError(f"Unable to read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise PDFInventoryError(f"JSON document must be an object: {path}")
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _write_text(path: Path, text: str) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(text, encoding="utf-8")
    os.replace(temporary_path, path)


def main() -> None:
    """Generate Phase 2 artifacts from an explicitly selected blank source PDF."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, default=DEFAULT_SCID_PDF_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_INVENTORY_DIR)
    parser.add_argument(
        "--source-manifest", type=Path, default=DEFAULT_SCID_SOURCE_MANIFEST_PATH
    )
    arguments = parser.parse_args()
    output = build_pdf_inventory(
        pdf_path=arguments.pdf,
        output_dir=arguments.output_dir,
        source_manifest_path=arguments.source_manifest,
    )
    print(output)


if __name__ == "__main__":
    main()
