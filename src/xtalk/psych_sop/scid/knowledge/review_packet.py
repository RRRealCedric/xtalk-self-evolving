"""Render source-traceable clinical review packets from a SCID knowledge bundle.

This module deliberately renders the reviewed JSON deterministically.  It does
not ask an LLM to paraphrase clinical content: reviewers see the stored source
prompt, source locator, structured interpretation, derived material, and flow
side by side.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .loader import DEFAULT_SCID_KNOWLEDGE_DIR, load_knowledge_bundle
from .schema import InterviewNode, KnowledgeBundle, TERMINAL_NODE_ID


_PROJECT_ROOT = Path(__file__).resolve().parents[6]
DEFAULT_SOURCE_PDF = (
    _PROJECT_ROOT / "psydata" / "realdata" / "PsychologySOP-Template" / "scid-5.pdf"
)
DEFAULT_REVIEW_PACKET_DIR = (
    _PROJECT_ROOT
    / "psydata"
    / "realdata"
    / "PsychologySOP-Template"
    / "scid-review-packets"
    / "g-pilot-v0.1.0"
)


@dataclass(frozen=True, slots=True)
class SourceCrop:
    """A normalized source-page crop associated with one or more nodes."""

    asset_id: str
    pdf_page: int
    printed_page: int | None
    normalized_box: tuple[int, int, int, int]
    node_ids: tuple[str, ...]
    caption: str


def build_review_packet(
    *,
    knowledge_root: str | Path | None = None,
    source_pdf: str | Path | None = None,
    output_dir: str | Path | None = None,
    render_images: bool = True,
) -> Path:
    """Build deterministic HTML, Markdown, flow, review-template, and crops.

    ``render_images`` exists for unit tests and environments without the
    optional local PDF-rendering dependency.  A production review packet should
    always render images from the frozen source PDF.
    """

    root = (
        Path(knowledge_root)
        if knowledge_root is not None
        else DEFAULT_SCID_KNOWLEDGE_DIR
    )
    output = Path(output_dir) if output_dir is not None else DEFAULT_REVIEW_PACKET_DIR
    source = Path(source_pdf) if source_pdf is not None else DEFAULT_SOURCE_PDF
    bundle = load_knowledge_bundle(root)
    layout = _load_json(root / "reviews" / "g-pilot-layout.json")
    review_spec = _load_json(root / "reviews" / "g-pilot-clinical-review.json")
    crops = _parse_crops(layout)

    output.mkdir(parents=True, exist_ok=True)
    image_dir = output / "images"
    if render_images:
        _render_source_crops(source, image_dir, crops)

    rendered_assets = {crop.asset_id: f"images/{crop.asset_id}.png" for crop in crops}
    (output / "clinical-review.md").write_text(
        _render_markdown(bundle, review_spec, crops, rendered_assets),
        encoding="utf-8",
    )
    (output / "index.html").write_text(
        _render_html(bundle, review_spec, crops, rendered_assets),
        encoding="utf-8",
    )
    (output / "flow.mmd").write_text(_render_mermaid(bundle), encoding="utf-8")
    (output / "review-template.json").write_text(
        json.dumps(
            _review_overlay_template(bundle, review_spec), ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "packet-manifest.json").write_text(
        json.dumps(
            {
                "packet_version": "1.0.0",
                "bundle_id": bundle.bundle_id,
                "content_version": bundle.content_version,
                "source_document": dict(bundle.source_document),
                "node_ids": list(bundle.nodes),
                "source_crops": [
                    {
                        "asset_id": crop.asset_id,
                        "pdf_page": crop.pdf_page,
                        "printed_page": crop.printed_page,
                        "node_ids": list(crop.node_ids),
                    }
                    for crop in crops
                ],
                "render_images": render_images,
                "review_status": bundle.content_review_status,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return output


def _render_source_crops(
    source_pdf: Path, image_dir: Path, crops: Iterable[SourceCrop]
) -> None:
    if not source_pdf.is_file():
        raise FileNotFoundError(f"Frozen SCID source PDF not found: {source_pdf}")
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - depends on local toolchain
        raise RuntimeError(
            "pypdfium2 and Pillow are required to render clinical review crops"
        ) from exc

    image_dir.mkdir(parents=True, exist_ok=True)
    document = pdfium.PdfDocument(str(source_pdf))
    pages: dict[int, Any] = {}
    for crop in crops:
        if crop.pdf_page not in pages:
            pages[crop.pdf_page] = (
                document[crop.pdf_page - 1].render(scale=2.5).to_pil()
            )
        image = pages[crop.pdf_page]
        left, top, right, bottom = _pixel_box(image.size, crop.normalized_box)
        image.crop((left, top, right, bottom)).save(image_dir / f"{crop.asset_id}.png")


def _pixel_box(
    size: tuple[int, int], normalized_box: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    width, height = size
    left, top, right, bottom = normalized_box
    pixels = (
        round(width * left / 10_000),
        round(height * top / 10_000),
        round(width * right / 10_000),
        round(height * bottom / 10_000),
    )
    if not (
        0 <= pixels[0] < pixels[2] <= width and 0 <= pixels[1] < pixels[3] <= height
    ):
        raise ValueError(f"Invalid normalized crop box: {normalized_box}")
    return pixels


def _render_markdown(
    bundle: KnowledgeBundle,
    review_spec: Mapping[str, Any],
    crops: tuple[SourceCrop, ...],
    assets: Mapping[str, str],
) -> str:
    lines = [
        "# SCID G 模块 Pilot 临床审核包",
        "",
        "## 审核边界",
        "",
        f"- Bundle：`{bundle.bundle_id}`",
        f"- Content version：`{bundle.content_version}`",
        f"- 当前状态：`{bundle.content_review_status}`",
        f"- 来源哈希：`{bundle.source_document.get('sha256', 'unknown')}`",
        "- 用途：工程 schema Pilot；不是完整 SCID 模块，不得用于诊断或真实用户访谈。",
        "- 本审核包由 JSON 确定性渲染；原题、页码、跳转和派生内容均单独显示。",
        "",
        "## 审核方法",
        "",
        "请对每个节点分别核对：来源忠实性、临床语义、评分资料要求、流程跳转和派生话术。",
        "不要仅因自然语言说明通顺而批准；应以原始 SCID 页面为准。",
        "",
        "## 流程概览",
        "",
        "```mermaid",
        _render_mermaid(bundle).rstrip(),
        "```",
        "",
    ]
    for node in bundle.nodes.values():
        lines.extend(_render_node_markdown(node, crops, assets))
    lines.extend(_render_global_review_items(review_spec))
    return "\n".join(lines) + "\n"


def _render_node_markdown(
    node: InterviewNode,
    crops: tuple[SourceCrop, ...],
    assets: Mapping[str, str],
) -> list[str]:
    classification = _node_classification(node)
    lines = [
        f"## 节点 {node.node_id}",
        "",
        f"- 类型：`{node.node_type}`",
        f"- 内容分类：`{classification}`",
        f"- 审核状态：`{node.review_status}`",
        f"- 临床意图：{node.clinical_intent}",
        f"- 时间范围：{node.time_window}",
        "",
        "### 原始来源",
        "",
    ]
    for source in node.source_refs:
        location = f"；PDF 第 {source.pdf_page} 页" if source.pdf_page else ""
        if source.printed_page:
            location += f"；印刷页 {source.printed_page}"
        if source.field_id:
            location += f"；字段 {source.field_id}"
        lines.append(
            f"- `{source.kind}`：`{source.locator}`{location}。{source.description}"
        )
    for crop in _crops_for_node(node.node_id, crops):
        asset = assets[crop.asset_id]
        lines.extend(
            [
                "",
                f"![{crop.caption}]({asset})",
                f"> {crop.caption}",
            ]
        )
    lines.extend(["", "### 原题或工程动作", ""])
    if node.canonical_prompt:
        lines.extend(["> " + node.canonical_prompt, ""])
    else:
        lines.extend(["> 本节点没有用户可见原题；它是工程性流程节点。", ""])

    lines.extend(["### 机器结构化含义", ""])
    lines.append("| 项目 | 内容 |")
    lines.append("| --- | --- |")
    lines.append(f"| 核心概念 | {node.dialogue_contract.core_concept} |")
    lines.append(f"| 阈值提醒 | {node.dialogue_contract.severity_threshold} |")
    lines.append(
        "| 不应视作 | "
        + ("；".join(node.dialogue_contract.key_exclusions) or "无")
        + " |"
    )
    lines.extend(["", "### 需要收集的证据", ""])
    if node.evidence_slots:
        lines.append("| Slot | 类型 | 含义 | 对应 score |")
        lines.append("| --- | --- | --- | --- |")
        for slot in node.evidence_slots:
            scores = ", ".join(slot.required_for_scores) or "不直接决定"
            lines.append(
                f"| `{slot.slot_id}` | `{slot.value_type}` | {slot.description} | {scores} |"
            )
    else:
        lines.append("本节点不收集新的临床 evidence slot。")

    lines.extend(["", "### 评分资料要求", ""])
    if node.score_requirements:
        lines.append("| Score | 最低资料要求 | Assessor 判断 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for requirement in node.score_requirements:
            slots = ", ".join(requirement.required_slots) or "无"
            judgment = "需要" if requirement.requires_assessor_judgment else "不需要"
            lines.append(
                f"| `{requirement.score}` | {slots} | {judgment} | {requirement.summary} |"
            )
    else:
        lines.append("本节点不产生 score。")

    lines.extend(["", "### 对话与澄清契约", ""])
    lines.append(
        "- 必须保留的信息："
        + ("、".join(node.dialogue_contract.required_information) or "无")
    )
    lines.append(
        "- 可允许的改写原则："
        + ("；".join(node.dialogue_contract.allowed_paraphrases) or "无")
    )
    if node.dialogue_contract.clarification_ladder:
        lines.append("- 澄清梯：")
        for step in node.dialogue_contract.clarification_ladder:
            lines.append(
                f"  {step.level}. 缺失 `{', '.join(step.when_missing)}`："
                f"`{step.strategy}`；{step.prompt_intent}"
            )
    else:
        lines.append("- 澄清梯：无。")

    lines.extend(["", "### 流程跳转", ""])
    lines.append("| 条件 | 目标 | Effect |")
    lines.append("| --- | --- | --- |")
    for transition in node.transitions:
        effects = (
            ", ".join(
                str(effect.get("type") or "unknown") for effect in transition.effects
            )
            or "无"
        )
        lines.append(
            f"| `{_condition_label(transition.when)}` | "
            f"`{transition.target_node_id}` | {effects} |"
        )

    lines.extend(
        [
            "",
            "### 临床审核结论",
            "",
            "- 来源忠实性：□ 通过　□ 需修改　□ 无法确认",
            "- 临床语义：□ 通过　□ 需修改　□ 无法确认",
            "- 评分资料要求：□ 通过　□ 需修改　□ 无法确认",
            "- 流程跳转：□ 通过　□ 需修改　□ 无法确认",
            "- 派生/对话内容：□ 通过　□ 需修改　□ 不适用",
            "- 审核意见：",
            "",
        ]
    )
    return lines


def _render_global_review_items(review_spec: Mapping[str, Any]) -> list[str]:
    lines = ["## 全局审核项目", ""]
    for item in review_spec.get("required_review_items", []):
        lines.append(f"- `{item['id']}`：{item['question']}　□ 通过　□ 需修改")
    lines.extend(
        [
            "",
            "## 审核签署",
            "",
            "- 审核者：",
            "- 专业资质/角色：",
            "- 日期：",
            "- 内容版本：",
            "- 总结结论：□ 批准　□ 附条件批准　□ 需修改后复审　□ 拒绝",
            "",
        ]
    )
    return lines


def _render_html(
    bundle: KnowledgeBundle,
    review_spec: Mapping[str, Any],
    crops: tuple[SourceCrop, ...],
    assets: Mapping[str, str],
) -> str:
    nav = "".join(
        f'<li><a href="#{html.escape(node.node_id)}">{html.escape(node.node_id)}</a></li>'
        for node in bundle.nodes.values()
    )
    cards = "".join(
        _render_node_html(node, crops, assets) for node in bundle.nodes.values()
    )
    review_items = "".join(
        "<li><code>{}</code>：{}</li>".format(
            html.escape(str(item["id"])), html.escape(str(item["question"]))
        )
        for item in review_spec.get("required_review_items", [])
    )
    flow = html.escape(_render_mermaid(bundle))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SCID G Pilot 临床审核包</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; max-width: 1100px; margin: 2rem auto; color: #1f2937; line-height: 1.65; padding: 0 1rem; }}
    nav {{ position: sticky; top: 0; background: white; border-bottom: 1px solid #d1d5db; padding: .7rem 0; }}
    nav ul {{ display: flex; flex-wrap: wrap; gap: .9rem; list-style: none; padding: 0; margin: 0; }}
    .notice {{ background: #fff7ed; border-left: 4px solid #f97316; padding: 1rem; }}
    .node {{ border: 1px solid #d1d5db; border-radius: .5rem; padding: 1.25rem; margin: 1.5rem 0; }}
    .derived {{ border-left: 5px solid #f59e0b; }} .engineering {{ border-left: 5px solid #64748b; }} .source {{ border-left: 5px solid #16a34a; }}
    table {{ border-collapse: collapse; width: 100%; margin: .7rem 0; }} th, td {{ border: 1px solid #d1d5db; padding: .5rem; vertical-align: top; text-align: left; }} th {{ background: #f3f4f6; }}
    img {{ max-width: 100%; border: 1px solid #9ca3af; margin: .5rem 0; }} code {{ overflow-wrap: anywhere; }} pre {{ white-space: pre-wrap; background: #f8fafc; padding: 1rem; }}
  </style>
</head>
<body>
  <h1>SCID G 模块 Pilot 临床审核包</h1>
  <div class="notice"><strong>审核边界：</strong>Bundle <code>{html.escape(bundle.bundle_id)}</code>，版本 <code>{html.escape(bundle.content_version)}</code>。这是一份工程 schema Pilot，不是完整 SCID 模块，不得用于诊断或真实用户访谈。所有派生内容均需临床审核。</div>
  <nav><ul>{nav}</ul></nav>
  <h2>流程概览</h2><pre>{flow}</pre>
  {cards}
  <h2>全局审核项目</h2><ul>{review_items}</ul>
  <h2>审核结论</h2><p>□ 批准　□ 附条件批准　□ 需修改后复审　□ 拒绝</p>
</body></html>"""


def _render_node_html(
    node: InterviewNode,
    crops: tuple[SourceCrop, ...],
    assets: Mapping[str, str],
) -> str:
    node_class = _node_classification(node)
    source_items = "".join(
        "<li><code>{}</code>：{}；{}</li>".format(
            html.escape(source.kind),
            html.escape(source.locator),
            html.escape(source.description),
        )
        for source in node.source_refs
    )
    images = "".join(
        '<figure><img src="{}" alt="{}"><figcaption>{}</figcaption></figure>'.format(
            html.escape(assets[crop.asset_id]),
            html.escape(crop.caption),
            html.escape(crop.caption),
        )
        for crop in _crops_for_node(node.node_id, crops)
    )
    slot_rows = (
        "".join(
            "<tr><td><code>{}</code></td><td>{}</td><td>{}</td></tr>".format(
                html.escape(slot.slot_id),
                html.escape(slot.value_type),
                html.escape(slot.description),
            )
            for slot in node.evidence_slots
        )
        or '<tr><td colspan="3">本节点不收集新的临床 evidence slot。</td></tr>'
    )
    score_rows = (
        "".join(
            "<tr><td><code>{}</code></td><td>{}</td><td>{}</td></tr>".format(
                html.escape(requirement.score),
                html.escape(", ".join(requirement.required_slots) or "无"),
                html.escape(requirement.summary),
            )
            for requirement in node.score_requirements
        )
        or '<tr><td colspan="3">本节点不产生 score。</td></tr>'
    )
    transition_rows = "".join(
        "<tr><td><code>{}</code></td><td><code>{}</code></td></tr>".format(
            html.escape(_condition_label(transition.when)),
            html.escape(transition.target_node_id),
        )
        for transition in node.transitions
    )
    ladder = (
        "".join(
            "<li>第 {} 层：缺失 <code>{}</code>；<code>{}</code>；{}</li>".format(
                step.level,
                html.escape(", ".join(step.when_missing)),
                html.escape(step.strategy),
                html.escape(step.prompt_intent),
            )
            for step in node.dialogue_contract.clarification_ladder
        )
        or "<li>无</li>"
    )
    prompt = html.escape(
        node.canonical_prompt or "本节点没有用户可见原题；它是工程性流程节点。"
    )
    return f"""
<section id="{html.escape(node.node_id)}" class="node {node_class}">
  <h2>{html.escape(node.node_id)} <small>({html.escape(node.node_type)})</small></h2>
  <p><strong>内容分类：</strong><code>{node_class}</code>　<strong>审核状态：</strong><code>{html.escape(node.review_status)}</code></p>
  <p><strong>临床意图：</strong>{html.escape(node.clinical_intent)}<br><strong>时间范围：</strong>{html.escape(node.time_window)}</p>
  <h3>原始来源</h3><ul>{source_items}</ul>{images}
  <h3>原题或工程动作</h3><blockquote>{prompt}</blockquote>
  <h3>机器结构化含义</h3>
  <p><strong>核心概念：</strong>{html.escape(node.dialogue_contract.core_concept)}<br><strong>阈值提醒：</strong>{html.escape(node.dialogue_contract.severity_threshold)}</p>
  <p><strong>不应视作：</strong>{html.escape('；'.join(node.dialogue_contract.key_exclusions) or '无')}</p>
  <h3>需要收集的证据</h3><table><tr><th>Slot</th><th>类型</th><th>含义</th></tr>{slot_rows}</table>
  <h3>评分资料要求</h3><table><tr><th>Score</th><th>最低资料</th><th>说明</th></tr>{score_rows}</table>
  <h3>澄清梯</h3><ol>{ladder}</ol>
  <h3>流程跳转</h3><table><tr><th>条件</th><th>目标</th></tr>{transition_rows}</table>
  <h3>临床审核结论</h3><p>来源 □通过 □需修改　语义 □通过 □需修改　评分 □通过 □需修改　流程 □通过 □需修改　派生话术 □通过 □需修改 □不适用</p><p>意见：</p>
</section>"""


def _render_mermaid(bundle: KnowledgeBundle) -> str:
    node_aliases = {node_id: f"N{index}" for index, node_id in enumerate(bundle.nodes)}
    lines = ["flowchart TD"]
    for node in bundle.nodes.values():
        lines.append(
            f'    {node_aliases[node.node_id]}["{node.node_id}: {node.node_type}"]'
        )
    lines.append('    END["$terminal"]')
    for node in bundle.nodes.values():
        for transition in node.transitions:
            target = (
                "END"
                if transition.target_node_id == TERMINAL_NODE_ID
                else node_aliases[transition.target_node_id]
            )
            lines.append(
                f"    {node_aliases[node.node_id]} -->|{_condition_label(transition.when)}| {target}"
            )
    return "\n".join(lines) + "\n"


def _review_overlay_template(
    bundle: KnowledgeBundle, review_spec: Mapping[str, Any]
) -> Mapping[str, Any]:
    clinical_nodes = [
        node
        for node in bundle.nodes.values()
        if _node_classification(node) != "engineering"
    ]
    return {
        "review_id": f"{bundle.bundle_id}-{bundle.content_version}-clinical-review",
        "bundle_id": bundle.bundle_id,
        "content_version": bundle.content_version,
        "reviewer": None,
        "reviewed_at": None,
        "overall_decision": "pending",
        "global_items": [
            {"id": item["id"], "decision": "pending", "comment": ""}
            for item in review_spec.get("required_review_items", [])
        ],
        "node_decisions": [
            {
                "node_id": node.node_id,
                "source_fidelity": "pending",
                "clinical_semantics": "pending",
                "score_requirements": "pending",
                "transitions": "pending",
                "derived_dialogue": (
                    "not_applicable"
                    if _node_classification(node) == "source"
                    else "pending"
                ),
                "comment": "",
            }
            for node in clinical_nodes
        ],
    }


def _parse_crops(layout: Mapping[str, Any]) -> tuple[SourceCrop, ...]:
    raw_crops = layout.get("source_crops")
    if not isinstance(raw_crops, list) or not raw_crops:
        raise ValueError("Review packet layout must define non-empty source_crops")
    parsed: list[SourceCrop] = []
    for raw in raw_crops:
        if not isinstance(raw, Mapping):
            raise ValueError("Review packet source crop must be an object")
        box = raw.get("normalized_box")
        if not (
            isinstance(box, list)
            and len(box) == 4
            and all(isinstance(value, int) for value in box)
        ):
            raise ValueError("Review packet crop must contain four integer coordinates")
        node_ids = raw.get("node_ids")
        if not isinstance(node_ids, list) or not all(
            isinstance(value, str) and value for value in node_ids
        ):
            raise ValueError("Review packet crop must contain node_ids")
        parsed.append(
            SourceCrop(
                asset_id=_required_text(raw.get("asset_id"), "asset_id"),
                pdf_page=_required_positive_int(raw.get("pdf_page"), "pdf_page"),
                printed_page=(
                    _required_positive_int(raw["printed_page"], "printed_page")
                    if raw.get("printed_page") is not None
                    else None
                ),
                normalized_box=tuple(box),  # type: ignore[arg-type]
                node_ids=tuple(node_ids),
                caption=_required_text(raw.get("caption"), "caption"),
            )
        )
    asset_ids = [crop.asset_id for crop in parsed]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("Review packet crop asset ids must be unique")
    return tuple(parsed)


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to load review-packet JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"Review-packet JSON must be an object: {path}")
    return value


def _crops_for_node(
    node_id: str, crops: Iterable[SourceCrop]
) -> tuple[SourceCrop, ...]:
    return tuple(crop for crop in crops if node_id in crop.node_ids)


def _node_classification(node: InterviewNode) -> str:
    kinds = {source.kind for source in node.source_refs}
    if kinds == {"engineering_scaffold"}:
        return "engineering"
    if "derived_clarification" in kinds:
        return "derived"
    return "source"


def _condition_label(condition: Mapping[str, Any]) -> str:
    if condition.get("always") is True:
        return "default"
    values = condition.get("score_in")
    if isinstance(values, (list, tuple)):
        # Mermaid treats braces as node-shape syntax even when they appear in
        # an edge label.  Keep labels deliberately plain and parser-safe.
        return "score: " + "、".join(str(value) for value in values)
    return json.dumps(dict(condition), ensure_ascii=False, sort_keys=True)


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Review-packet {name} must be a non-empty string")
    return value.strip()


def _required_positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"Review-packet {name} must be a positive integer")
    return value
