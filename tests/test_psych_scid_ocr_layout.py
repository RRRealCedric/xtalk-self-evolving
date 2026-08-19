from pathlib import Path

from xtalk.psych_sop.scid.knowledge.ocr_layout import (
    DEFAULT_SOURCE_MAP_DIR,
    load_ocr_layout,
    render_ocr_layout_report,
)


def test_committed_phase_three_source_map_is_complete_and_reviewable():
    rendered, ocr_blocks, quality, alignment = load_ocr_layout()

    assert [page["page_number"] for page in rendered["pages"]] == [4, 232, 233]
    assert ocr_blocks["ocr_engine"]["language"] == "chi_sim+eng"
    assert (
        ocr_blocks["review_status"] == "machine_extracted_pending_human_source_review"
    )
    assert quality["summary"]["review_queue_status"] == "queued_for_human_review"
    assert quality["summary"]["critical_review_queue_count"] > 0

    for page in ocr_blocks["pages"]:
        assert page["quality"]["word_count"] > 0
        assert page["quality"]["line_count"] > 0
        assert [line["reading_order"] for line in page["lines"]] == list(
            range(1, len(page["lines"]) + 1)
        )
        assert all(
            "\n" not in word["text"] and "\t" not in word["text"]
            for line in page["lines"]
            for word in line["words"]
        )
        assert all(
            line["layout_role_candidate"].endswith("_candidate")
            for line in page["lines"]
        )
        assert page["quality"]["layout_role_candidate_counts"]

    assert all(asset["ocr_line_count"] > 0 for asset in alignment["layout_assets"])
    assert {anchor["node_id"] for anchor in alignment["form_field_anchors"]} >= {
        "S9",
        "S12",
        "G3",
        "G6",
        "G7",
        "G11",
    }
    assert all(
        Path(_project_path(item["image_path"])).exists()
        for item in alignment["visual_overlays"]
    )


def test_phase_three_report_is_deterministic_from_committed_artifacts():
    rendered, ocr_blocks, quality, alignment = load_ocr_layout()

    report = render_ocr_layout_report(
        render_manifest=rendered,
        ocr_blocks=ocr_blocks,
        quality=quality,
        alignment=alignment,
    )
    committed = (DEFAULT_SOURCE_MAP_DIR / "g-pilot-ocr-layout-report.md").read_text(
        encoding="utf-8"
    )
    assert report == committed


def _project_path(relative_path: str) -> Path:
    return Path(__file__).resolve().parents[2] / relative_path
