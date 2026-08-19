"""Phase 3 SCID Pilot rendering, local OCR, and structural layout alignment.

This module produces source-layer artifacts only.  OCR text, geometry, and
reading order are deliberately kept separate from clinical nodes, scoring, and
runtime behavior.  Every OCR result remains pending human source review.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .pdf_inventory import (
    DEFAULT_INVENTORY_DIR,
    DEFAULT_SCID_KNOWLEDGE_DIR,
    DEFAULT_SCID_PDF_PATH,
    PDFInventoryError,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[6]
DEFAULT_SOURCE_MAP_DIR = DEFAULT_SCID_KNOWLEDGE_DIR / "source-map"
DEFAULT_RENDERED_PAGE_DIR = DEFAULT_SOURCE_MAP_DIR / "rendered" / "g-pilot-pages"
DEFAULT_ALIGNMENT_OVERLAY_DIR = DEFAULT_SOURCE_MAP_DIR / "alignment-overlays"
DEFAULT_PILOT_LOCATIONS_PATH = DEFAULT_INVENTORY_DIR / "scid-5.pilot-locations.json"
DEFAULT_INVENTORY_PATH = DEFAULT_INVENTORY_DIR / "scid-5.acroform-inventory.json"
DEFAULT_REVIEW_LAYOUT_PATH = (
    DEFAULT_SCID_KNOWLEDGE_DIR / "reviews" / "g-pilot-layout.json"
)

RENDER_DPI = 300
OCR_LANGUAGE = "chi_sim+eng"
OCR_PAGE_SEGMENTATION_MODE = 6
LOW_CONFIDENCE_THRESHOLD = 60.0
CRITICAL_REVIEW_THRESHOLD = 85.0
SOURCE_MAP_VERSION = "1.0.0"
GENERATOR_VERSION = "1.0.0"

_CRITICAL_TOKEN_PATTERNS: dict[str, tuple[str, ...]] = {
    "negation": ("不", "无", "没", "否", "未"),
    "time_window": (
        "一生",
        "任何",
        "最近",
        "过去",
        "终生",
        "持续",
        "个月",
        "年",
        "月",
        "周",
    ),
    "score_or_number": ("?", "？", "1", "2", "3"),
}


class OCRLayoutError(ValueError):
    """Raised when the local render/OCR source pipeline cannot be completed."""


def build_ocr_layout(
    *,
    pdf_path: str | Path = DEFAULT_SCID_PDF_PATH,
    inventory_path: str | Path = DEFAULT_INVENTORY_PATH,
    pilot_locations_path: str | Path = DEFAULT_PILOT_LOCATIONS_PATH,
    review_layout_path: str | Path = DEFAULT_REVIEW_LAYOUT_PATH,
    output_dir: str | Path = DEFAULT_SOURCE_MAP_DIR,
    dpi: int = RENDER_DPI,
    language: str = OCR_LANGUAGE,
    page_segmentation_mode: int = OCR_PAGE_SEGMENTATION_MODE,
    tesseract_binary: str = "tesseract",
) -> Path:
    """Render Pilot source pages, run local OCR, and write Phase 3 artifacts."""

    if dpi <= 0:
        raise OCRLayoutError("OCR render DPI must be positive")
    if page_segmentation_mode < 0 or page_segmentation_mode > 13:
        raise OCRLayoutError("Tesseract page segmentation mode must be in [0, 13]")

    source_path = Path(pdf_path)
    inventory = _load_json_object(Path(inventory_path))
    pilot_locations = _load_json_object(Path(pilot_locations_path))
    review_layout = _load_json_object(Path(review_layout_path))
    source_sha256 = _sha256(source_path)
    inventory_sha256 = _required_string(
        inventory.get("source_document", {}).get("sha256"),
        "inventory.source_document.sha256",
    )
    if source_sha256 != inventory_sha256:
        raise OCRLayoutError(
            "OCR source PDF hash does not match the Phase 2 inventory: "
            f"expected {inventory_sha256}, got {source_sha256}"
        )

    output = Path(output_dir)
    page_dir = output / "rendered" / "g-pilot-pages"
    overlay_dir = output / "alignment-overlays"
    output.mkdir(parents=True, exist_ok=True)
    page_dir.mkdir(parents=True, exist_ok=True)
    overlay_dir.mkdir(parents=True, exist_ok=True)

    page_numbers = _pilot_page_numbers(review_layout)
    geometry = _load_pdf_geometry(source_path, page_numbers)
    rendered_pages = _render_pages(
        source_path,
        page_numbers=page_numbers,
        geometry=geometry,
        output_dir=page_dir,
        dpi=dpi,
    )
    tesseract = _resolve_tesseract(tesseract_binary, language)
    render_manifest = {
        "render_manifest_version": SOURCE_MAP_VERSION,
        "generator": {
            "name": "xtalk.scid.ocr_layout",
            "version": GENERATOR_VERSION,
            "renderer": "pypdfium2",
            "dpi": dpi,
        },
        "source_document": {
            "relative_path": _project_relative_path(source_path),
            "sha256": source_sha256,
        },
        "pages": rendered_pages,
        "coordinate_systems": {
            "pdf_widget": "PDF user space points, origin lower-left in the unrotated crop box",
            "rendered_image": "pixels, origin upper-left",
            "review_layout": "normalized 0-10000, origin upper-left in rendered-page space",
        },
    }
    render_manifest_path = output / "g-pilot-render-manifest.json"
    _write_json(render_manifest_path, render_manifest)

    ocr_pages = [
        _ocr_page(
            page=page,
            tesseract=tesseract,
            language=language,
            page_segmentation_mode=page_segmentation_mode,
        )
        for page in rendered_pages
    ]
    ocr_blocks = {
        "ocr_blocks_version": SOURCE_MAP_VERSION,
        "source_document_sha256": source_sha256,
        "render_manifest_sha256": _sha256(render_manifest_path),
        "ocr_engine": {
            "binary": tesseract["path"],
            "version": tesseract["version"],
            "language": language,
            "page_segmentation_mode": page_segmentation_mode,
            "output_format": "tsv",
        },
        "reading_order_contract": {
            "strategy": "geometric_top_then_left_with_tesseract_location_tiebreaker",
            "meaning": "A stable visual review order only; it is not a clinical, conversational, or routing order.",
        },
        "pages": ocr_pages,
        "review_status": "machine_extracted_pending_human_source_review",
        "limitations": [
            "OCR text is an unadjudicated source-layer derivative and must not replace the frozen PDF.",
            "Tesseract block and paragraph boundaries are retained for traceability; they are not semantic SCID sections.",
        ],
    }
    ocr_blocks_path = output / "g-pilot-ocr-blocks.json"
    _write_json(ocr_blocks_path, ocr_blocks)

    quality = _build_quality_report(ocr_blocks)
    quality_path = output / "g-pilot-ocr-quality.json"
    _write_json(quality_path, quality)

    alignment = _build_alignment(
        inventory=inventory,
        pilot_locations=pilot_locations,
        review_layout=review_layout,
        rendered_pages=rendered_pages,
        ocr_pages=ocr_pages,
        source_sha256=source_sha256,
        render_manifest_sha256=_sha256(render_manifest_path),
        ocr_blocks_sha256=_sha256(ocr_blocks_path),
    )
    overlays = _render_alignment_overlays(
        alignment,
        rendered_pages=rendered_pages,
        output_dir=overlay_dir,
    )
    alignment["visual_overlays"] = overlays
    alignment_path = output / "g-pilot-anchor-alignment.json"
    _write_json(alignment_path, alignment)

    validate_ocr_layout(
        render_manifest=render_manifest,
        ocr_blocks=ocr_blocks,
        quality=quality,
        alignment=alignment,
    )
    report = render_ocr_layout_report(
        render_manifest=render_manifest,
        ocr_blocks=ocr_blocks,
        quality=quality,
        alignment=alignment,
    )
    _write_text(output / "g-pilot-ocr-layout-report.md", report)
    return output


def load_ocr_layout(
    output_dir: str | Path = DEFAULT_SOURCE_MAP_DIR,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load and validate the four machine-readable Phase 3 artifacts."""

    directory = Path(output_dir)
    render_manifest = _load_json_object(directory / "g-pilot-render-manifest.json")
    ocr_blocks = _load_json_object(directory / "g-pilot-ocr-blocks.json")
    quality = _load_json_object(directory / "g-pilot-ocr-quality.json")
    alignment = _load_json_object(directory / "g-pilot-anchor-alignment.json")
    validate_ocr_layout(
        render_manifest=render_manifest,
        ocr_blocks=ocr_blocks,
        quality=quality,
        alignment=alignment,
    )
    return render_manifest, ocr_blocks, quality, alignment


def validate_ocr_layout(
    *,
    render_manifest: Mapping[str, Any],
    ocr_blocks: Mapping[str, Any],
    quality: Mapping[str, Any],
    alignment: Mapping[str, Any],
) -> None:
    """Check source-layer completeness without asserting clinical correctness."""

    pages = render_manifest.get("pages")
    ocr_pages = ocr_blocks.get("pages")
    if not isinstance(pages, list) or not isinstance(ocr_pages, list):
        raise OCRLayoutError("Render manifest and OCR blocks must both contain pages")
    page_numbers = [
        item.get("page_number") for item in pages if isinstance(item, Mapping)
    ]
    if page_numbers != sorted(page_numbers) or len(page_numbers) != len(
        set(page_numbers)
    ):
        raise OCRLayoutError("Rendered page numbers must be unique and sorted")
    if page_numbers != [4, 232, 233]:
        raise OCRLayoutError(f"Unexpected Phase 3 Pilot pages: {page_numbers}")
    ocr_by_page = {
        item.get("page_number"): item for item in ocr_pages if isinstance(item, Mapping)
    }
    if set(ocr_by_page) != set(page_numbers):
        raise OCRLayoutError("OCR pages do not match rendered Pilot pages")
    for rendered in pages:
        width = rendered.get("image_width_px")
        height = rendered.get("image_height_px")
        if (
            not isinstance(width, int)
            or not isinstance(height, int)
            or width <= 0
            or height <= 0
        ):
            raise OCRLayoutError("Rendered page has invalid dimensions")
        ocr_page = ocr_by_page[rendered["page_number"]]
        lines = ocr_page.get("lines")
        if not isinstance(lines, list) or not lines:
            raise OCRLayoutError(f"OCR page {rendered['page_number']} has no lines")
        order = [
            line.get("reading_order") for line in lines if isinstance(line, Mapping)
        ]
        if order != list(range(1, len(lines) + 1)):
            raise OCRLayoutError(
                f"OCR page {rendered['page_number']} has invalid reading order"
            )
        for line in lines:
            _validate_image_rect(line.get("bbox_image_px"), width, height)
            for word in line.get("words", []):
                _validate_image_rect(word.get("bbox_image_px"), width, height)
    summary = quality.get("summary")
    if (
        not isinstance(summary, Mapping)
        or summary.get("review_queue_status") != "queued_for_human_review"
    ):
        raise OCRLayoutError("OCR quality report must preserve a human-review queue")
    layout_assets = alignment.get("layout_assets")
    if not isinstance(layout_assets, list) or not layout_assets:
        raise OCRLayoutError("Alignment must contain reviewed layout assets")
    if any(
        item.get("ocr_line_count", 0) == 0
        for item in layout_assets
        if isinstance(item, Mapping)
    ):
        raise OCRLayoutError(
            "Every reviewed layout asset must align with at least one OCR line"
        )


def render_ocr_layout_report(
    *,
    render_manifest: Mapping[str, Any],
    ocr_blocks: Mapping[str, Any],
    quality: Mapping[str, Any],
    alignment: Mapping[str, Any],
) -> str:
    """Render a concise, deterministic Phase 3 review report."""

    source = render_manifest["source_document"]
    engine = ocr_blocks["ocr_engine"]
    summary = quality["summary"]
    page_rows = "\n".join(
        f"| {page['page_number']} | {page['image_width_px']} x {page['image_height_px']} | "
        f"{_ocr_page_by_number(ocr_blocks, page['page_number'])['quality']['word_count']} | "
        f"{_ocr_page_by_number(ocr_blocks, page['page_number'])['quality']['line_count']} |"
        for page in render_manifest["pages"]
    )
    asset_rows = "\n".join(
        f"| `{asset['asset_id']}` | {asset['pdf_page']} | {asset['ocr_line_count']} |"
        for asset in alignment["layout_assets"]
    )
    return "\n".join(
        [
            "# G Pilot OCR and Layout Report",
            "",
            "## Scope",
            "",
            "This report records local OCR and geometric alignment for the G Pilot source pages. It is source-layer evidence, not a clinical interpretation or a runtime bundle.",
            "",
            "## Reproducibility",
            "",
            f"- Source SHA-256: `{source['sha256']}`",
            f"- Renderer: `{render_manifest['generator']['renderer']}` at {render_manifest['generator']['dpi']} DPI",
            f"- OCR engine: `{engine['binary']}` ({engine['version']})",
            f"- OCR language / PSM: `{engine['language']}` / `{engine['page_segmentation_mode']}`",
            "",
            "## Rendered pages",
            "",
            "| PDF page | Pixels | OCR words | OCR lines |",
            "| ---: | --- | ---: | ---: |",
            page_rows,
            "",
            "## OCR quality queue",
            "",
            f"- Low-confidence words (< {LOW_CONFIDENCE_THRESHOLD:g}): {summary['low_confidence_word_count']}",
            f"- Critical-token review entries (< {CRITICAL_REVIEW_THRESHOLD:g}): {summary['critical_review_queue_count']}",
            f"- Queue status: `{summary['review_queue_status']}`",
            "",
            "## Layout-source alignment",
            "",
            "| Layout asset | PDF page | Intersecting OCR lines |",
            "| --- | ---: | ---: |",
            asset_rows,
            "",
            "## Interpretation boundary",
            "",
            "Tesseract text, confidence, reading order, and geometry require human source review. The artifact does not assign constructs, scores, criteria, diagnoses, or runtime transitions.",
            "",
        ]
    )


def _render_pages(
    source_path: Path,
    *,
    page_numbers: list[int],
    geometry: Mapping[int, Mapping[str, Any]],
    output_dir: Path,
    dpi: int,
) -> list[dict[str, Any]]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError(
            "pypdfium2 is required to render SCID OCR source pages"
        ) from exc

    document = pdfium.PdfDocument(str(source_path))
    pages: list[dict[str, Any]] = []
    for page_number in page_numbers:
        page = document[page_number - 1]
        image = page.render(scale=dpi / 72).to_pil().convert("RGB")
        image_path = output_dir / f"scid5-page-{page_number:03d}-{dpi}dpi.png"
        image.save(image_path, format="PNG")
        pages.append(
            {
                "page_number": page_number,
                "image_path": _project_relative_path(image_path),
                "image_sha256": _sha256(image_path),
                "image_width_px": image.width,
                "image_height_px": image.height,
                "dpi": dpi,
                "pdf_geometry": geometry[page_number],
            }
        )
    return pages


def _ocr_page(
    *,
    page: Mapping[str, Any],
    tesseract: Mapping[str, str],
    language: str,
    page_segmentation_mode: int,
) -> dict[str, Any]:
    image_path = _resolve_project_path(
        _required_string(page.get("image_path"), "image_path")
    )
    command = [
        tesseract["path"],
        str(image_path),
        "stdout",
        "-l",
        language,
        "--psm",
        str(page_segmentation_mode),
        "tsv",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise OCRLayoutError(
            "Tesseract failed for page "
            f"{page['page_number']}: {completed.stderr.strip()[:2048]}"
        )
    lines, blocks, quality = _parse_tesseract_tsv(
        completed.stdout,
        page_number=int(page["page_number"]),
        image_width=int(page["image_width_px"]),
        image_height=int(page["image_height_px"]),
    )
    return {
        "page_number": page["page_number"],
        "image_path": page["image_path"],
        "ocr_blocks": blocks,
        "lines": lines,
        "quality": quality,
        "tesseract_stderr_notice_count": len(
            [line for line in completed.stderr.splitlines() if line.strip()]
        ),
    }


def _parse_tesseract_tsv(
    payload: str,
    *,
    page_number: int,
    image_width: int,
    image_height: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    # Tesseract TSV does not CSV-escape OCR text.  In particular, an OCR token
    # may itself begin with a quotation mark, so the stdlib CSV default would
    # incorrectly consume later TSV rows as one quoted field.
    rows = csv.DictReader(io.StringIO(payload), delimiter="\t", quoting=csv.QUOTE_NONE)
    lines_by_key: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    source_order = 0
    for row in rows:
        if row.get("level") != "5":
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        source_order += 1
        block_number = _integer(row.get("block_num"), "block_num")
        paragraph_number = _integer(row.get("par_num"), "par_num")
        line_number = _integer(row.get("line_num"), "line_num")
        left = _integer(row.get("left"), "left")
        top = _integer(row.get("top"), "top")
        width = _integer(row.get("width"), "width")
        height = _integer(row.get("height"), "height")
        confidence = _float(row.get("conf"), "conf")
        rect = [left, top, left + width, top + height]
        _validate_image_rect(rect, image_width, image_height)
        word_id = f"scid5:p{page_number:03d}:ocr-word:{source_order:05d}"
        word = {
            "word_id": word_id,
            "text": text,
            "bbox_image_px": rect,
            "confidence": confidence,
            "tesseract_location": {
                "block_number": block_number,
                "paragraph_number": paragraph_number,
                "line_number": line_number,
                "word_number": _integer(row.get("word_num"), "word_num"),
            },
        }
        lines_by_key[(block_number, paragraph_number, line_number)].append(word)

    line_records: list[dict[str, Any]] = []
    for (block_number, paragraph_number, line_number), words in lines_by_key.items():
        words.sort(key=lambda word: (word["bbox_image_px"][0], word["word_id"]))
        rect = _union_rect(word["bbox_image_px"] for word in words)
        confidences = [word["confidence"] for word in words]
        line_records.append(
            {
                "line_id": f"scid5:p{page_number:03d}:ocr-line:{block_number:03d}-{paragraph_number:03d}-{line_number:03d}",
                "text": " ".join(word["text"] for word in words),
                "bbox_image_px": rect,
                "bbox_normalized": _normalized_rect(rect, image_width, image_height),
                "confidence": {
                    "mean": round(sum(confidences) / len(confidences), 6),
                    "minimum": round(min(confidences), 6),
                    "low_confidence_word_count": sum(
                        value < LOW_CONFIDENCE_THRESHOLD for value in confidences
                    ),
                },
                "tesseract_location": {
                    "block_number": block_number,
                    "paragraph_number": paragraph_number,
                    "line_number": line_number,
                },
                "word_ids": [word["word_id"] for word in words],
                "words": words,
            }
        )
    line_records.sort(
        key=lambda line: (
            line["bbox_image_px"][1],
            line["bbox_image_px"][0],
            line["tesseract_location"]["block_number"],
            line["tesseract_location"]["paragraph_number"],
            line["tesseract_location"]["line_number"],
        )
    )
    for reading_order, line in enumerate(line_records, start=1):
        line["reading_order"] = reading_order
        line["layout_role_candidate"] = _layout_role_candidate(
            page_number=page_number,
            rect=line["bbox_image_px"],
            text=line["text"],
            image_width=image_width,
            image_height=image_height,
        )

    blocks_by_key: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for line in line_records:
        location = line["tesseract_location"]
        blocks_by_key[(location["block_number"], location["paragraph_number"])].append(
            line
        )
    block_records: list[dict[str, Any]] = []
    for (block_number, paragraph_number), lines in blocks_by_key.items():
        block_records.append(
            {
                "ocr_block_id": f"scid5:p{page_number:03d}:ocr-block:{block_number:03d}-{paragraph_number:03d}",
                "bbox_image_px": _union_rect(line["bbox_image_px"] for line in lines),
                "bbox_normalized": _normalized_rect(
                    _union_rect(line["bbox_image_px"] for line in lines),
                    image_width,
                    image_height,
                ),
                "tesseract_location": {
                    "block_number": block_number,
                    "paragraph_number": paragraph_number,
                },
                "line_ids": [
                    line["line_id"]
                    for line in sorted(lines, key=lambda item: item["reading_order"])
                ],
                "first_reading_order": min(line["reading_order"] for line in lines),
            }
        )
    block_records.sort(
        key=lambda block: (block["first_reading_order"], block["ocr_block_id"])
    )

    all_words = [word for line in line_records for word in line["words"]]
    critical_queue = [
        {
            "word_id": word["word_id"],
            "text": word["text"],
            "confidence": word["confidence"],
            "categories": _critical_categories(word["text"]),
            "bbox_image_px": word["bbox_image_px"],
        }
        for word in all_words
        if _critical_categories(word["text"])
        and word["confidence"] < CRITICAL_REVIEW_THRESHOLD
    ]
    category_counts = {
        category: sum(
            category in _critical_categories(word["text"]) for word in all_words
        )
        for category in _CRITICAL_TOKEN_PATTERNS
    }
    quality = {
        "word_count": len(all_words),
        "line_count": len(line_records),
        "ocr_block_count": len(block_records),
        "low_confidence_word_count": sum(
            word["confidence"] < LOW_CONFIDENCE_THRESHOLD for word in all_words
        ),
        "critical_token_observed_counts": category_counts,
        "layout_role_candidate_counts": dict(
            sorted(
                {
                    role: sum(
                        line["layout_role_candidate"] == role for line in line_records
                    )
                    for role in {line["layout_role_candidate"] for line in line_records}
                }.items()
            )
        ),
        "critical_low_confidence_words": critical_queue,
        "review_status": "machine_extracted_pending_human_source_review",
    }
    if not line_records:
        raise OCRLayoutError(f"Tesseract produced no OCR lines for page {page_number}")
    return line_records, block_records, quality


def _build_quality_report(ocr_blocks: Mapping[str, Any]) -> dict[str, Any]:
    pages = ocr_blocks.get("pages", [])
    queue: list[dict[str, Any]] = []
    page_summaries: list[dict[str, Any]] = []
    for page in pages:
        quality = page["quality"]
        queue.extend(
            {"page_number": page["page_number"], **item}
            for item in quality["critical_low_confidence_words"]
        )
        page_summaries.append(
            {
                "page_number": page["page_number"],
                "word_count": quality["word_count"],
                "line_count": quality["line_count"],
                "ocr_block_count": quality["ocr_block_count"],
                "low_confidence_word_count": quality["low_confidence_word_count"],
                "critical_token_observed_counts": quality[
                    "critical_token_observed_counts"
                ],
                "layout_role_candidate_counts": quality["layout_role_candidate_counts"],
            }
        )
    return {
        "quality_report_version": SOURCE_MAP_VERSION,
        "source_document_sha256": ocr_blocks["source_document_sha256"],
        "thresholds": {
            "low_confidence": LOW_CONFIDENCE_THRESHOLD,
            "critical_token_review": CRITICAL_REVIEW_THRESHOLD,
        },
        "summary": {
            "page_count": len(page_summaries),
            "word_count": sum(item["word_count"] for item in page_summaries),
            "line_count": sum(item["line_count"] for item in page_summaries),
            "low_confidence_word_count": sum(
                item["low_confidence_word_count"] for item in page_summaries
            ),
            "critical_review_queue_count": len(queue),
            "review_queue_status": "queued_for_human_review",
        },
        "pages": page_summaries,
        "critical_token_review_queue": queue,
        "limitations": [
            "Confidence is a Tesseract signal, not a clinical confidence score.",
            "The queue identifies items for source review; it does not adjudicate OCR correctness.",
        ],
    }


def _build_alignment(
    *,
    inventory: Mapping[str, Any],
    pilot_locations: Mapping[str, Any],
    review_layout: Mapping[str, Any],
    rendered_pages: list[Mapping[str, Any]],
    ocr_pages: list[Mapping[str, Any]],
    source_sha256: str,
    render_manifest_sha256: str,
    ocr_blocks_sha256: str,
) -> dict[str, Any]:
    rendered_by_page = {item["page_number"]: item for item in rendered_pages}
    lines_by_page = {item["page_number"]: item["lines"] for item in ocr_pages}
    layout_assets: list[dict[str, Any]] = []
    for asset in review_layout.get("source_crops", []):
        page_number = asset.get("pdf_page")
        rendered = rendered_by_page.get(page_number)
        if rendered is None:
            raise OCRLayoutError(
                f"Review asset references an unrendered page: {page_number}"
            )
        image_rect = _layout_rect_to_image(
            asset.get("normalized_box"),
            image_width=rendered["image_width_px"],
            image_height=rendered["image_height_px"],
        )
        overlapping = _intersecting_line_ids(lines_by_page[page_number], image_rect)
        layout_assets.append(
            {
                "asset_id": asset.get("asset_id"),
                "pdf_page": page_number,
                "printed_page": asset.get("printed_page"),
                "node_ids": asset.get("node_ids", []),
                "normalized_box": asset.get("normalized_box"),
                "image_rect_px": image_rect,
                "ocr_line_count": len(overlapping),
                "ocr_line_ids": overlapping,
            }
        )

    inventory_fields = {
        item.get("stable_id"): item
        for item in inventory.get("fields", [])
        if isinstance(item, Mapping) and isinstance(item.get("stable_id"), str)
    }
    field_anchors: list[dict[str, Any]] = []
    for location in pilot_locations.get("locations", []):
        if not isinstance(location, Mapping):
            continue
        node_id = location.get("node_id")
        layout_pages = {
            asset["pdf_page"]
            for asset in layout_assets
            if node_id in asset.get("node_ids", [])
        }
        for source_ref in location.get("source_refs", []):
            if (
                not isinstance(source_ref, Mapping)
                or "matched_stable_id" not in source_ref
            ):
                continue
            field = inventory_fields.get(source_ref["matched_stable_id"])
            if field is None:
                raise OCRLayoutError(
                    f"Pilot location references missing inventory field {source_ref['matched_stable_id']}"
                )
            declared_page = source_ref.get("declared_pdf_page")
            relevant_pages = (
                {declared_page} if isinstance(declared_page, int) else layout_pages
            )
            for widget in field.get("widgets", []):
                page_number = widget.get("page_number")
                if (
                    page_number not in relevant_pages
                    or page_number not in rendered_by_page
                ):
                    continue
                rendered = rendered_by_page[page_number]
                image_rect = _pdf_rect_to_image(
                    widget.get("rect_pdf_points"),
                    rendered["pdf_geometry"],
                    image_width=rendered["image_width_px"],
                    image_height=rendered["image_height_px"],
                )
                field_anchors.append(
                    {
                        "node_id": node_id,
                        "source_kind": source_ref.get("source_kind"),
                        "requested_field_id": source_ref.get("requested_field_id"),
                        "match_status": source_ref.get("status"),
                        "relation_note": source_ref.get("relation_note"),
                        "matched_field_id": field["canonical_name"],
                        "matched_stable_id": field["stable_id"],
                        "widget_object_ref": widget.get("object_ref"),
                        "page_number": page_number,
                        "pdf_rect_points": widget.get("rect_pdf_points"),
                        "image_rect_px": image_rect,
                        "nearest_ocr_line_ids": _nearest_line_ids(
                            lines_by_page[page_number], image_rect, limit=5
                        ),
                    }
                )
    field_anchors = _deduplicate_anchors(field_anchors)
    return {
        "alignment_version": SOURCE_MAP_VERSION,
        "source_document_sha256": source_sha256,
        "inventory_sha256": inventory.get("source_document", {}).get("sha256"),
        "render_manifest_sha256": render_manifest_sha256,
        "ocr_blocks_sha256": ocr_blocks_sha256,
        "coordinate_transform": {
            "method": "crop_box_and_page_rotation_to_rendered_image",
            "note": "The transform is geometric only; it does not assert field or OCR semantics.",
        },
        "layout_assets": layout_assets,
        "form_field_anchors": field_anchors,
        "limitations": [
            "An intersecting or nearest OCR line is a review aid, not proof of a semantic field-to-text relationship.",
            "related_field_match retains the Phase 2 distinction between a static source region and an exact AcroForm field.",
        ],
    }


def _render_alignment_overlays(
    alignment: Mapping[str, Any],
    *,
    rendered_pages: Iterable[Mapping[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required to create OCR alignment overlays"
        ) from exc
    assets_by_page: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    anchors_by_page: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for asset in alignment["layout_assets"]:
        assets_by_page[asset["pdf_page"]].append(asset)
    for anchor in alignment["form_field_anchors"]:
        anchors_by_page[anchor["page_number"]].append(anchor)
    overlays: list[dict[str, Any]] = []
    for page in rendered_pages:
        page_number = page["page_number"]
        image = Image.open(_resolve_project_path(page["image_path"])).convert("RGB")
        drawing = ImageDraw.Draw(image)
        for asset in assets_by_page[page_number]:
            drawing.rectangle(asset["image_rect_px"], outline=(0, 102, 204), width=5)
        for anchor in anchors_by_page[page_number]:
            drawing.rectangle(anchor["image_rect_px"], outline=(220, 30, 30), width=4)
        output_path = output_dir / f"scid5-page-{page_number:03d}-alignment-overlay.png"
        image.save(output_path, format="PNG")
        overlays.append(
            {
                "page_number": page_number,
                "image_path": _project_relative_path(output_path),
                "image_sha256": _sha256(output_path),
                "legend": {
                    "blue": "reviewed source-region crop from g-pilot-layout.json",
                    "red": "Phase 2 form-field anchor widget",
                },
            }
        )
    return overlays


def _load_pdf_geometry(
    source_path: Path, page_numbers: Iterable[int]
) -> dict[int, dict[str, Any]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is required to read SCID PDF geometry") from exc
    reader = PdfReader(str(source_path))
    geometry: dict[int, dict[str, Any]] = {}
    for page_number in page_numbers:
        page = reader.pages[page_number - 1]
        crop_box = page.cropbox
        rotation = int(page.get("/Rotate", 0)) % 360
        if rotation not in {0, 90, 180, 270}:
            raise OCRLayoutError(f"Unsupported PDF page rotation: {rotation}")
        geometry[page_number] = {
            "crop_box_pdf_points": [
                float(crop_box.left),
                float(crop_box.bottom),
                float(crop_box.right),
                float(crop_box.top),
            ],
            "rotation_degrees": rotation,
        }
    return geometry


def _resolve_tesseract(binary: str, language: str) -> dict[str, str]:
    resolved = shutil.which(binary)
    if resolved is None:
        raise OCRLayoutError(f"Tesseract binary is unavailable: {binary}")
    version_run = subprocess.run(
        [resolved, "--version"], capture_output=True, text=True, check=False
    )
    if version_run.returncode != 0:
        raise OCRLayoutError("Unable to inspect Tesseract version")
    version = next(
        (line.strip() for line in version_run.stdout.splitlines() if line.strip()),
        "unknown",
    )
    languages_run = subprocess.run(
        [resolved, "--list-langs"], capture_output=True, text=True, check=False
    )
    if languages_run.returncode != 0:
        raise OCRLayoutError("Unable to inspect installed Tesseract language data")
    available_languages = {
        line.strip()
        for line in languages_run.stdout.splitlines()
        if line.strip() and not line.startswith("List of available languages")
    }
    requested_languages = set(language.split("+"))
    missing = sorted(requested_languages - available_languages)
    if missing:
        raise OCRLayoutError(f"Tesseract language data is unavailable: {missing}")
    return {"path": resolved, "version": version}


def _pilot_page_numbers(review_layout: Mapping[str, Any]) -> list[int]:
    pages = sorted(
        {
            item.get("pdf_page")
            for item in review_layout.get("source_crops", [])
            if isinstance(item, Mapping) and isinstance(item.get("pdf_page"), int)
        }
    )
    if pages != [4, 232, 233]:
        raise OCRLayoutError(f"Unexpected G Pilot layout pages: {pages}")
    return pages


def _pdf_rect_to_image(
    rect: Any,
    geometry: Mapping[str, Any],
    *,
    image_width: int,
    image_height: int,
) -> list[int]:
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        raise OCRLayoutError("PDF widget rectangle must contain four coordinates")
    left, bottom, right, top = (float(value) for value in rect)
    crop_left, crop_bottom, crop_right, crop_top = geometry["crop_box_pdf_points"]
    crop_width = crop_right - crop_left
    crop_height = crop_top - crop_bottom
    rotation = geometry["rotation_degrees"]
    if crop_width <= 0 or crop_height <= 0:
        raise OCRLayoutError("PDF crop box dimensions must be positive")

    def transform(x: float, y: float) -> tuple[float, float]:
        dx, dy = x - crop_left, y - crop_bottom
        if rotation == 0:
            return (
                dx / crop_width * image_width,
                (crop_height - dy) / crop_height * image_height,
            )
        if rotation == 90:
            return dy / crop_height * image_width, dx / crop_width * image_height
        if rotation == 180:
            return (
                crop_width - dx
            ) / crop_width * image_width, dy / crop_height * image_height
        return (crop_height - dy) / crop_height * image_width, (
            crop_width - dx
        ) / crop_width * image_height

    points = [transform(x, y) for x in (left, right) for y in (bottom, top)]
    return _integer_rect(
        [
            min(point[0] for point in points),
            min(point[1] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
        ],
        image_width,
        image_height,
    )


def _layout_rect_to_image(
    value: Any, *, image_width: int, image_height: int
) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise OCRLayoutError(
            "Review-layout normalized box must contain four coordinates"
        )
    left, top, right, bottom = (float(item) for item in value)
    return _integer_rect(
        [
            left / 10000 * image_width,
            top / 10000 * image_height,
            right / 10000 * image_width,
            bottom / 10000 * image_height,
        ],
        image_width,
        image_height,
    )


def _intersecting_line_ids(
    lines: Iterable[Mapping[str, Any]], rect: list[int]
) -> list[str]:
    return [
        line["line_id"]
        for line in lines
        if _rects_intersect(line["bbox_image_px"], rect)
    ]


def _nearest_line_ids(
    lines: Iterable[Mapping[str, Any]], rect: list[int], *, limit: int
) -> list[str]:
    center_x = (rect[0] + rect[2]) / 2
    center_y = (rect[1] + rect[3]) / 2
    ranked = sorted(
        (
            (
                math.hypot(
                    (line["bbox_image_px"][0] + line["bbox_image_px"][2]) / 2
                    - center_x,
                    (line["bbox_image_px"][1] + line["bbox_image_px"][3]) / 2
                    - center_y,
                ),
                line["reading_order"],
                line["line_id"],
            )
            for line in lines
        )
    )
    return [item[2] for item in ranked[:limit]]


def _deduplicate_anchors(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        key = (
            item["node_id"],
            item["requested_field_id"],
            item["matched_stable_id"],
            item["widget_object_ref"],
            item["page_number"],
        )
        unique.setdefault(key, dict(item))
    return [unique[key] for key in sorted(unique)]


def _critical_categories(text: str) -> list[str]:
    return [
        category
        for category, tokens in _CRITICAL_TOKEN_PATTERNS.items()
        if any(token in text for token in tokens)
    ]


def _layout_role_candidate(
    *,
    page_number: int,
    rect: list[int],
    text: str,
    image_width: int,
    image_height: int,
) -> str:
    """Return a non-semantic G-Pilot layout role candidate for human review."""

    center_x = (rect[0] + rect[2]) / 2 / image_width
    center_y = (rect[1] + rect[3]) / 2 / image_height
    compact_text = text.replace(" ", "")
    if center_y >= 0.90:
        return "footer_or_score_legend_candidate"
    if center_y <= 0.12:
        return "header_candidate"
    if "跳至" in compact_text or "接下页" in compact_text:
        return "navigation_instruction_candidate"
    if center_x >= 0.75:
        return "score_or_navigation_candidate"
    if page_number in {232, 233} and center_x >= 0.38:
        return "criterion_or_interviewer_instruction_candidate"
    return "question_or_prompt_candidate"


def _ocr_page_by_number(
    ocr_blocks: Mapping[str, Any], page_number: int
) -> Mapping[str, Any]:
    for page in ocr_blocks["pages"]:
        if page["page_number"] == page_number:
            return page
    raise OCRLayoutError(f"Missing OCR page {page_number}")


def _rects_intersect(left: Iterable[float], right: Iterable[float]) -> bool:
    left_box = list(left)
    right_box = list(right)
    return not (
        left_box[2] < right_box[0]
        or right_box[2] < left_box[0]
        or left_box[3] < right_box[1]
        or right_box[3] < left_box[1]
    )


def _union_rect(rectangles: Iterable[Iterable[float]]) -> list[int]:
    values = [list(rectangle) for rectangle in rectangles]
    if not values:
        raise OCRLayoutError("Cannot union an empty list of rectangles")
    return [
        min(int(rectangle[0]) for rectangle in values),
        min(int(rectangle[1]) for rectangle in values),
        max(int(rectangle[2]) for rectangle in values),
        max(int(rectangle[3]) for rectangle in values),
    ]


def _normalized_rect(rect: Iterable[float], width: int, height: int) -> list[int]:
    values = list(rect)
    return [
        round(values[0] / width * 10000),
        round(values[1] / height * 10000),
        round(values[2] / width * 10000),
        round(values[3] / height * 10000),
    ]


def _integer_rect(rect: Iterable[float], width: int, height: int) -> list[int]:
    values = list(rect)
    result = [
        max(0, min(width, round(values[0]))),
        max(0, min(height, round(values[1]))),
        max(0, min(width, round(values[2]))),
        max(0, min(height, round(values[3]))),
    ]
    if result[2] < result[0] or result[3] < result[1]:
        raise OCRLayoutError("Image rectangle has inverted bounds")
    return result


def _validate_image_rect(rect: Any, width: int, height: int) -> None:
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        raise OCRLayoutError("OCR rectangle must contain four coordinates")
    left, top, right, bottom = rect
    if not all(isinstance(value, (int, float)) for value in rect):
        raise OCRLayoutError("OCR rectangle contains nonnumeric values")
    if not (0 <= left <= right <= width and 0 <= top <= bottom <= height):
        raise OCRLayoutError(f"OCR rectangle exceeds image bounds: {rect}")


def _integer(value: Any, name: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise OCRLayoutError(f"Invalid Tesseract TSV {name}: {value!r}") from exc


def _float(value: Any, name: str) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise OCRLayoutError(f"Invalid Tesseract TSV {name}: {value!r}") from exc


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise OCRLayoutError(f"Missing required string {name}")
    return value


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OCRLayoutError(f"Unable to read JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise OCRLayoutError(f"JSON document must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _project_relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(_PROJECT_ROOT))
    except ValueError:
        return str(path)


def _resolve_project_path(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _PROJECT_ROOT / candidate


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _write_text(path: Path, text: str) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(text, encoding="utf-8")
    os.replace(temporary_path, path)


def main() -> None:
    """Generate local Phase 3 artifacts from the frozen blank source PDF."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, default=DEFAULT_SCID_PDF_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_SOURCE_MAP_DIR)
    parser.add_argument("--dpi", type=int, default=RENDER_DPI)
    parser.add_argument("--language", default=OCR_LANGUAGE)
    parser.add_argument("--psm", type=int, default=OCR_PAGE_SEGMENTATION_MODE)
    parser.add_argument("--tesseract", default="tesseract")
    arguments = parser.parse_args()
    output = build_ocr_layout(
        pdf_path=arguments.pdf,
        output_dir=arguments.output_dir,
        dpi=arguments.dpi,
        language=arguments.language,
        page_segmentation_mode=arguments.psm,
        tesseract_binary=arguments.tesseract,
    )
    print(output)


if __name__ == "__main__":
    main()
