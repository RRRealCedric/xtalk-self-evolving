import json

from xtalk.psych_sop.scid.knowledge import build_review_packet


def test_review_packet_renders_traceable_clinician_artifacts(tmp_path):
    output = build_review_packet(output_dir=tmp_path, render_images=False)

    markdown = (output / "clinical-review.md").read_text(encoding="utf-8")
    html = (output / "index.html").read_text(encoding="utf-8")
    flow = (output / "flow.mmd").read_text(encoding="utf-8")
    template = json.loads((output / "review-template.json").read_text())
    manifest = json.loads((output / "packet-manifest.json").read_text())

    assert "PDF 第 232 页" in markdown
    assert "G3" in markdown
    assert "derived" in markdown
    assert "工程性流程节点" in markdown
    assert "原始来源" in html
    assert "S9.POSITIVE_TO_G3" not in flow
    assert "score: 3" in flow
    assert "{" not in flow
    assert manifest["render_images"] is False
    assert template["overall_decision"] == "pending"
    assert {item["node_id"] for item in template["node_decisions"]} >= {
        "S9",
        "G3",
        "G6",
        "G7",
        "S12",
        "G11",
    }
