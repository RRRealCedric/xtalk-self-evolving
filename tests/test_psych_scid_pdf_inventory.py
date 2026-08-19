import hashlib
import json
from pathlib import Path

from xtalk.psych_sop.scid.knowledge.pdf_inventory import (
    DEFAULT_INVENTORY_DIR,
    EXPECTED_ACROFORM_FIELD_COUNT,
    build_anomaly_report,
    build_pilot_locations,
    load_pdf_inventory,
    render_inventory_report,
)


def test_committed_inventory_covers_every_acroform_field_and_resolves_goto():
    inventory = load_pdf_inventory()

    assert (
        inventory["coverage"]["inventoried_field_count"]
        == EXPECTED_ACROFORM_FIELD_COUNT
    )
    assert inventory["coverage"]["field_coverage_ratio"] == 1.0
    assert inventory["action_summary"]["goto_resolved"] == 1014
    assert inventory["action_summary"]["goto_errors"] == 0
    assert not inventory["parse_errors"]

    fields = inventory["fields"]
    assert (
        len({field["stable_id"] for field in fields}) == EXPECTED_ACROFORM_FIELD_COUNT
    )
    assert (
        len({field["canonical_name"] for field in fields})
        == EXPECTED_ACROFORM_FIELD_COUNT
    )
    assert all(
        field["stable_id"].startswith("scid5-zh-local-source-01:acroform:")
        for field in fields
    )


def test_committed_pilot_locations_preserve_exact_and_related_anchor_distinction():
    inventory = load_pdf_inventory()
    locations = build_pilot_locations(inventory)
    by_node = {item["node_id"]: item for item in locations["locations"]}

    s9_pdf_anchor = next(
        item
        for item in by_node["S9"]["source_refs"]
        if item["source_kind"] == "pdf_form_anchor"
    )
    assert s9_pdf_anchor["status"] == "exact_field_match"
    assert s9_pdf_anchor["declared_page_present"] is True

    g3 = by_node["G3"]["source_refs"][0]
    g11 = by_node["G11"]["source_refs"][0]
    assert g3["status"] == "related_field_match"
    assert g3["matched_field_id"] == "S9-G3"
    assert g11["status"] == "related_field_match"
    assert g11["matched_field_id"] == "S12-G11"
    assert by_node["G.PILOT.OBSESSION_RETURN"]["status"] == "not_applicable"


def test_inventory_reports_are_deterministic_from_committed_json():
    inventory_path = DEFAULT_INVENTORY_DIR / "scid-5.acroform-inventory.json"
    raw = inventory_path.read_bytes()
    inventory = json.loads(raw)
    locations = build_pilot_locations(inventory)
    anomalies = build_anomaly_report(inventory, locations)

    report_one = render_inventory_report(inventory, anomalies)
    report_two = render_inventory_report(
        json.loads(raw), build_anomaly_report(json.loads(raw), locations)
    )
    assert report_one == report_two
    assert (
        hashlib.sha256(raw).hexdigest()
        == hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    )
