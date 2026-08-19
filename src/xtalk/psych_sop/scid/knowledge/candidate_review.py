"""Build and validate a hash-bound clinical review workspace for SCID candidates.

The Phase 5 review record is deliberately separate from the generated
candidate bundle.  Candidate generation can be repeated as source extraction
improves, while a reviewer decision remains attached to the exact candidate
SHA-256 that was reviewed.  This module does not certify any clinical content
and cannot publish candidate material to the runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from .candidate_generation import DEFAULT_CANDIDATES_DIR, load_candidate_bundle


_PROJECT_ROOT = Path(__file__).resolve().parents[6]
DEFAULT_REVIEW_RECORD_PATH = DEFAULT_CANDIDATES_DIR / "g-pilot-candidate-review-v2.json"
DEFAULT_REVIEW_WORKSPACE_DIR = (
    _PROJECT_ROOT
    / "psydata"
    / "realdata"
    / "PsychologySOP-Template"
    / "scid-review-packets"
    / "g-pilot-v0.1.0"
    / "phase-5-candidate-review"
)

REVIEW_RECORD_VERSION = "1.0.0"
REVIEWER_DECISIONS = frozenset(
    {"approve", "approve_with_changes", "reject", "needs_adjudication"}
)
REVIEWER_ROLES = frozenset(
    {"clinical_reviewer", "engineering_reviewer", "source_mapper", "adjudicator"}
)
ITEM_STATUSES = frozenset(
    {
        "pending",
        "in_review",
        "approved",
        "changes_requested",
        "needs_adjudication",
    }
)
RECORD_STATUSES = frozenset(
    {"open", "in_review", "needs_adjudication", "ready_for_compiler"}
)


class CandidateReviewError(ValueError):
    """Raised when a candidate review record is malformed or unsafe."""


def initialize_g_pilot_review_record(
    *,
    candidate_path: str | Path = DEFAULT_CANDIDATES_DIR / "g-pilot-candidates.json",
    output_path: str | Path = DEFAULT_REVIEW_RECORD_PATH,
    overwrite: bool = False,
) -> Path:
    """Create an empty, hash-bound Phase 5 review record.

    Parameters
    ----------
    candidate_path
        Non-published candidate bundle to be reviewed.
    output_path
        New review-overlay location.  Existing records are protected unless
        ``overwrite`` is explicitly selected for a disposable workspace.
    overwrite
        Whether a pre-existing record may be replaced.  Production review
        records should never use this option because that would erase audit
        evidence.
    """

    destination = Path(output_path)
    if destination.exists() and not overwrite:
        raise CandidateReviewError(
            f"Review record already exists and will not be overwritten: {destination}"
        )
    candidate_bundle = load_candidate_bundle(candidate_path)
    record = build_review_record(candidate_bundle)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_json(destination, record)
    return destination


def build_review_record(candidate_bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Build an empty review record with a deterministic semantic worklist."""

    candidate_id = _required_text(
        candidate_bundle.get("candidate_bundle_id"), "candidate_bundle_id"
    )
    candidate_sha = _canonical_sha256(candidate_bundle)
    items = [
        item
        for candidate in candidate_bundle["candidates"]
        for item in _candidate_review_items(candidate)
    ]
    global_items = _global_review_items()
    return {
        "review_record_version": REVIEW_RECORD_VERSION,
        "review_id": "scid5-zh-g-pilot-phase5-review-v1",
        "record_status": "open",
        "candidate_bundle_id": candidate_id,
        "candidate_bundle_sha256": candidate_sha,
        "candidate_status_required": "candidate_only_not_published",
        "scope": "G Pilot only: S9/S12 to G3/G6/G7/G11; not a complete SCID module.",
        "release_boundary": (
            "This record authorizes neither publication nor Runtime use. A later "
            "compiler, static validation, governance, and deployment gate remains required."
        ),
        "reviewer_decision_values": sorted(REVIEWER_DECISIONS),
        "recording_requirements": {
            "reviewer_id_rule": "Use a stable controlled reviewer identifier; store credential evidence outside this source repository.",
            "approval_rule": "An approval is valid only when the exact candidate hash matches and the reviewer supplies a decision date and evidence note.",
            "independence_rule": "Where two approvals are required, they must be from distinct reviewer_id values.",
            "change_rule": "approve_with_changes, reject, or needs_adjudication blocks release. Correct source/module content first, regenerate candidates, then begin a new hash-bound review record.",
        },
        "review_item_schema": {
            "reviewer_decision": {
                "reviewer_id": "required stable controlled identifier",
                "reviewer_role": sorted(REVIEWER_ROLES - {"adjudicator"}),
                "decision": sorted(REVIEWER_DECISIONS),
                "reviewed_on": "YYYY-MM-DD",
                "evidence_note": "required concise rationale with source locator or review-packet reference",
                "proposed_change": "optional; required for approve_with_changes or reject",
            },
            "adjudication": {
                "status": ["not_required", "pending", "resolved"],
                "adjudicator_id": "required only when resolved",
                "decided_on": "YYYY-MM-DD required only when resolved",
                "decision": ["approve", "approve_with_changes", "reject"],
                "rationale": "required only when resolved",
            },
        },
        "global_review_items": global_items,
        "review_items": items,
        "review_summary": _initial_summary(items, global_items),
    }


def load_candidate_review_record(
    path: str | Path = DEFAULT_REVIEW_RECORD_PATH,
) -> dict[str, Any]:
    """Load one Phase 5 review record without granting publication authority."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateReviewError(
            f"Unable to read candidate review record: {source}"
        ) from exc
    if not isinstance(payload, dict):
        raise CandidateReviewError("Candidate review record must be a JSON object")
    return payload


def validate_candidate_review_record(
    record: Mapping[str, Any],
    candidate_bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a Phase 5 review record and return a release-readiness summary.

    The returned summary is intentionally conservative.  It can report that a
    record is structurally complete, but it never changes candidate content or
    marks it as released.
    """

    if record.get("review_record_version") != REVIEW_RECORD_VERSION:
        raise CandidateReviewError("Unsupported candidate review record version")
    if record.get("candidate_bundle_id") != candidate_bundle.get("candidate_bundle_id"):
        raise CandidateReviewError("Review record candidate bundle ID does not match")
    candidate_sha = _canonical_sha256(candidate_bundle)
    if record.get("candidate_bundle_sha256") != candidate_sha:
        raise CandidateReviewError(
            "Review record hash does not match the candidate bundle; approvals cannot carry across a regeneration"
        )
    if candidate_bundle.get("candidate_status") != record.get(
        "candidate_status_required"
    ):
        raise CandidateReviewError(
            "Review record may only target non-published candidates"
        )
    record_status = record.get("record_status")
    if record_status not in RECORD_STATUSES:
        raise CandidateReviewError(f"Invalid review record status: {record_status!r}")

    expected = build_review_record(candidate_bundle)
    expected_items = {
        item["review_item_id"]: item
        for item in [*expected["global_review_items"], *expected["review_items"]]
    }
    actual_items = {
        item.get("review_item_id"): item
        for item in [
            *_require_list(record.get("global_review_items"), "global_review_items"),
            *_require_list(record.get("review_items"), "review_items"),
        ]
        if isinstance(item, Mapping)
    }
    if set(actual_items) != set(expected_items):
        missing = sorted(set(expected_items) - set(actual_items))
        extra = sorted(set(actual_items) - set(expected_items))
        raise CandidateReviewError(
            f"Review record worklist differs from candidate bundle; missing={missing}, extra={extra}"
        )

    item_statuses: Counter[str] = Counter()
    blockers: list[str] = []
    for item_id, expected_item in expected_items.items():
        item = actual_items[item_id]
        _validate_immutable_item_shape(item, expected_item)
        state = _validate_item_decisions(item)
        item_statuses[state] += 1
        if state != "approved" and item["release_blocking"]:
            blockers.append(item_id)

    declared_summary = record.get("review_summary")
    if not isinstance(declared_summary, Mapping):
        raise CandidateReviewError("Review record must contain review_summary")
    computed = {
        "total_items": len(expected_items),
        "item_status_counts": dict(sorted(item_statuses.items())),
        "release_blocking_open_item_ids": blockers,
        "ready_for_compiler": not blockers,
        "candidate_stays_non_published": True,
    }
    if dict(declared_summary) != computed:
        raise CandidateReviewError(
            "review_summary is stale; regenerate it from the reviewer decisions before validation"
        )
    if record_status == "ready_for_compiler" and not computed["ready_for_compiler"]:
        raise CandidateReviewError(
            "Record cannot be ready_for_compiler while blocking items remain"
        )
    return computed


def refresh_review_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy with a derived summary after a reviewer edits decisions.

    This is a mechanical helper only.  It does not validate credentials,
    adjudicate disagreement, or convert any candidate into released content.
    """

    refreshed = json.loads(json.dumps(record, ensure_ascii=False))
    items = [*refreshed["global_review_items"], *refreshed["review_items"]]
    counts: Counter[str] = Counter()
    blockers: list[str] = []
    for item in items:
        state = _item_status(item)
        item["item_status"] = state
        counts[state] += 1
        if item["release_blocking"] and state != "approved":
            blockers.append(item["review_item_id"])
    refreshed["review_summary"] = {
        "total_items": len(items),
        "item_status_counts": dict(sorted(counts.items())),
        "release_blocking_open_item_ids": blockers,
        "ready_for_compiler": not blockers,
        "candidate_stays_non_published": True,
    }
    if blockers:
        refreshed["record_status"] = (
            "needs_adjudication" if counts.get("needs_adjudication", 0) else "in_review"
        )
    else:
        refreshed["record_status"] = "ready_for_compiler"
    return refreshed


def refresh_g_pilot_review_record(
    *,
    candidate_path: str | Path = DEFAULT_CANDIDATES_DIR / "g-pilot-candidates.json",
    review_record_path: str | Path = DEFAULT_REVIEW_RECORD_PATH,
) -> Path:
    """Validate and persist only derived review status after manual decisions.

    This helper is deliberately unable to alter candidate data.  If the edited
    record is malformed, stale, or refers to a regenerated candidate hash, it
    fails before writing anything.
    """

    candidate_bundle = load_candidate_bundle(candidate_path)
    record_path = Path(review_record_path)
    refreshed = refresh_review_summary(load_candidate_review_record(record_path))
    validate_candidate_review_record(refreshed, candidate_bundle)
    _write_json(record_path, refreshed)
    return record_path


def build_g_pilot_review_workspace(
    *,
    candidate_path: str | Path = DEFAULT_CANDIDATES_DIR / "g-pilot-candidates.json",
    review_record_path: str | Path = DEFAULT_REVIEW_RECORD_PATH,
    output_dir: str | Path = DEFAULT_REVIEW_WORKSPACE_DIR,
) -> Path:
    """Render a local, human-readable Phase 5 review workspace.

    The output is a deterministic inspection aid.  Reviewers write decisions
    into the separate JSON record; this HTML/Markdown workspace never changes
    clinical source content or runtime configuration.
    """

    candidate_bundle = load_candidate_bundle(candidate_path)
    record = load_candidate_review_record(review_record_path)
    summary = validate_candidate_review_record(record, candidate_bundle)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "review-worklist.md").write_text(
        render_review_worklist(candidate_bundle, record, summary), encoding="utf-8"
    )
    (output / "index.html").write_text(
        render_review_dashboard(candidate_bundle, record, summary), encoding="utf-8"
    )
    _write_json(output / "review-readiness.json", summary)
    _write_json(
        output / "workspace-manifest.json",
        {
            "workspace_version": REVIEW_RECORD_VERSION,
            "candidate_bundle_id": candidate_bundle["candidate_bundle_id"],
            "candidate_bundle_sha256": _canonical_sha256(candidate_bundle),
            "review_record_path": _project_relative_path(Path(review_record_path)),
            "review_record_sha256": _sha256(Path(review_record_path)),
            "status": record["record_status"],
            "candidate_status": candidate_bundle["candidate_status"],
            "candidate_stays_non_published": True,
        },
    )
    return output


def render_review_worklist(
    candidate_bundle: Mapping[str, Any],
    record: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> str:
    """Render the candidate review record as a readable Markdown worklist."""

    items_by_node: dict[str, list[Mapping[str, Any]]] = {}
    for item in record["review_items"]:
        items_by_node.setdefault(item["candidate_node_id"], []).append(item)
    lines = [
        "# SCID G Pilot 阶段五候选审核工作台",
        "",
        "## 边界与当前状态",
        "",
        f"- 候选包：`{candidate_bundle['candidate_bundle_id']}`",
        f"- 候选哈希：`{record['candidate_bundle_sha256']}`",
        f"- 审核记录：`{record['review_id']}`，状态：`{record['record_status']}`",
        f"- 审核项：{summary['total_items']}；未关闭的 release-blocking 项：{len(summary['release_blocking_open_item_ids'])}",
        "- 本文件是可读审核入口；正式结论写入 `g-pilot-candidate-review-v2.json`，并运行校验器刷新汇总。",
        "- 所有内容仍为 `candidate_only_not_published`：不得复制到 Runtime、released bundle 或真实用户访谈。",
        "",
        "## 审核方式",
        "",
        "每一项只判断一个临床或工程命题，而不是逐行阅读 JSON。对照候选项列出的来源线索、原题/工程动作和流程信息后，录入审核者标识、角色、日期、结论与简短依据。`approve_with_changes`、`reject` 和 `needs_adjudication` 都会阻断后续阶段；修订内容后必须重新生成候选并开启新的哈希绑定记录。",
        "",
        "## 全局项",
        "",
    ]
    for item in record["global_review_items"]:
        lines.extend(_render_worklist_item_markdown(item))
    for candidate in candidate_bundle["candidates"]:
        node_id = candidate["candidate_node_id"]
        proposal = candidate["proposal"]
        lines.extend(
            [
                f"## 节点 {node_id}",
                "",
                f"- 类型：`{proposal['node_type']}`；来源置信度：`{candidate['source_confidence']}`",
                f"- 临床意图候选：{proposal['clinical_intent_candidate']}",
                f"- 时间范围候选：`{proposal['time_window_candidate']}`",
                "",
                "### 原题或工程动作",
                "",
                "> "
                + (_canonical_prompt(proposal) or "工程控制节点，无用户可见原题。"),
                "",
                "### 来源定位",
                "",
            ]
        )
        for source in candidate["source_evidence"]["knowledge_source_refs"]:
            page = f"；PDF 第 {source['pdf_page']} 页" if source["pdf_page"] else ""
            field = f"；字段 {source['field_id']}" if source["field_id"] else ""
            lines.append(
                f"- `{source['kind']}`：`{source['locator']}`{page}{field}。{source['description']}"
            )
        lines.extend(["", "### 评分与跳转候选", ""])
        score_labels = (
            "; ".join(
                f"score {score['score']}：{score['summary']}"
                for score in proposal["score_requirement_candidates"]
            )
            or "本节点不评分。"
        )
        lines.append(f"- 评分：{score_labels}")
        for transition in proposal["transition_candidates"]:
            lines.append(
                f"- `{transition['transition_id']}`：`{_condition_label(transition['when'])}` → `{transition['target_node_id']}`"
            )
        lines.extend(["", "### 待审语义项", ""])
        for item in items_by_node[node_id]:
            lines.extend(_render_worklist_item_markdown(item))
    lines.extend(
        [
            "## 阶段五出口",
            "",
            "只有所有 release-blocking 项为 `approved`、所需独立审核人数满足、争议完成裁决，且审核记录仍与候选包哈希一致时，记录才可显示 `ready_for_compiler`。即使满足该条件，也仅可进入阶段六 Compiler 与静态校验，不能发布到 Runtime。",
            "",
        ]
    )
    return "\n".join(lines)


def render_review_dashboard(
    candidate_bundle: Mapping[str, Any],
    record: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> str:
    """Render a static local HTML dashboard for Phase 5 reviewers."""

    cards = "".join(
        _render_candidate_card(candidate, record["review_items"])
        for candidate in candidate_bundle["candidates"]
    )
    global_cards = "".join(
        _render_item_html(item) for item in record["global_review_items"]
    )
    status = html.escape(str(record["record_status"]))
    open_count = len(summary["release_blocking_open_item_ids"])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SCID G Pilot 阶段五候选审核</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; max-width: 1180px; margin: 2rem auto; color: #1f2937; line-height: 1.65; padding: 0 1rem; }}
    .notice {{ background: #fff7ed; border-left: 4px solid #f97316; padding: 1rem; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: .75rem; margin: 1rem 0; }}
    .metric, .item, .candidate {{ border: 1px solid #d1d5db; border-radius: .5rem; padding: 1rem; background: #fff; }}
    .candidate {{ margin: 1.25rem 0; }} .item {{ margin: .75rem 0; }} .high {{ border-left: 5px solid #dc2626; }} .routine {{ border-left: 5px solid #2563eb; }} .engineering {{ border-left: 5px solid #64748b; }}
    details {{ margin: .65rem 0; }} summary {{ cursor: pointer; font-weight: 600; }} table {{ border-collapse: collapse; width: 100%; margin: .7rem 0; }} th, td {{ border: 1px solid #d1d5db; padding: .5rem; vertical-align: top; text-align: left; }} th {{ background: #f3f4f6; }} code {{ overflow-wrap: anywhere; }} blockquote {{ margin: .5rem 0; padding: .5rem 1rem; border-left: 3px solid #9ca3af; background: #f8fafc; }}
  </style>
</head>
<body>
  <h1>SCID G Pilot 阶段五候选审核</h1>
  <div class="notice"><strong>非发布边界：</strong>本工作台展示的内容均为候选，不能用于 Runtime、诊断或真实用户访谈。临床结论必须在独立 JSON 审核记录中签署；本页面不保存输入。</div>
  <div class="summary"><div class="metric"><strong>审核状态</strong><br><code>{status}</code></div><div class="metric"><strong>审核项</strong><br>{summary['total_items']}</div><div class="metric"><strong>未关闭阻断项</strong><br>{open_count}</div><div class="metric"><strong>候选哈希</strong><br><code>{html.escape(record['candidate_bundle_sha256'])}</code></div></div>
  <h2>全局审核项</h2>{global_cards}
  <h2>节点语义审核包</h2>{cards}
  <h2>记录规则</h2><p>每项的批准数量、角色和独立性由 JSON record 强制校验。若候选内容被再生成、哈希改变，旧批准不可迁移。出现改动请求、拒绝或分歧时，须先修订候选并启动新的审核记录或由指定裁决者记录裁决。</p>
</body></html>"""


def _candidate_review_items(candidate: Mapping[str, Any]) -> list[dict[str, Any]]:
    proposal = candidate["proposal"]
    node_id = candidate["candidate_node_id"]
    node_type = proposal["node_type"]
    source_confidence = candidate["source_confidence"]
    items: list[dict[str, Any]] = []
    if node_type in {"module_summary", "return"}:
        return [
            _new_item(
                candidate,
                dimension="engineering_control",
                risk_tier="engineering",
                question="该工程控制节点是否仍然是非诊断、无用户可见原题的流程支架，并且只执行所列出的返回/汇总作用？",
                required_approvals=[
                    _approval("engineering_reviewer", 1),
                    _approval("clinical_reviewer", 1),
                ],
            )
        ]
    source_approvals = [_approval("clinical_reviewer", 1)]
    source_risk = "routine"
    if source_confidence == "medium":
        source_approvals = [_approval("clinical_reviewer", 2)]
        source_risk = "high"
    items.append(
        _new_item(
            candidate,
            dimension="source_fidelity",
            risk_tier=source_risk,
            question="原题、页码/字段锚点和候选来源关系是否被如实呈现；是否明确区分 exact field、related field 和派生内容？",
            required_approvals=source_approvals,
        )
    )
    items.append(
        _new_item(
            candidate,
            dimension="clinical_semantics",
            risk_tier="routine",
            question="临床概念、时间范围和排除边界是否忠实于来源，且没有加入来源未支持的 criterion 或诊断结论？",
            required_approvals=[_approval("clinical_reviewer", 1)],
        )
    )
    if proposal["score_requirement_candidates"]:
        items.append(
            _new_item(
                candidate,
                dimension="slots_and_scores",
                risk_tier="high",
                question="证据槽及各 score 的最低资料要求是否准确，并且仅作为资料充分性而非自主诊断规则？",
                required_approvals=[_approval("clinical_reviewer", 2)],
            )
        )
    items.append(
        _new_item(
            candidate,
            dimension="transitions",
            risk_tier="high",
            question="每条跳转的条件、目标和 effect 是否与限定 Pilot 的非诊断流程一致，且不存在因默认分支绕过必要澄清或安全边界？",
            required_approvals=[
                _approval("clinical_reviewer", 1),
                _approval("engineering_reviewer", 1),
            ],
        )
    )
    expressions = proposal["natural_language_expression_candidates"]
    if expressions or node_type == "evidence_probe":
        is_derived = node_type == "evidence_probe"
        items.append(
            _new_item(
                candidate,
                dimension="expression",
                risk_tier="high" if is_derived else "routine",
                question=(
                    "派生澄清是否非诱导、保留既定时间范围，并在不确定时允许记录 unknown 而不是重复原题？"
                    if is_derived
                    else "用户可见问法是否保留核心概念、时间范围和排除边界，且不把普通体验病理化？"
                ),
                required_approvals=[
                    _approval("clinical_reviewer", 2 if is_derived else 1)
                ],
            )
        )
    return items


def _global_review_items() -> list[dict[str, Any]]:
    return [
        _new_global_item(
            review_item_id="GLOBAL.phase3_source_audit_traceability",
            dimension="source_audit_traceability",
            risk_tier="high",
            question="阶段三已由项目负责人报告完成，但逐项 reviewer overlay、身份和更正记录尚未存档；是否已补充或明确保留这一审计证据缺口？",
            required_approvals=[_approval("source_mapper", 1)],
        ),
        _new_global_item(
            review_item_id="GLOBAL.runtime_publication_boundary",
            dimension="publication_boundary",
            risk_tier="engineering",
            question="是否确认所有 G Pilot 制品仍为 candidate_only_not_published，且未被复制到 released bundle、Runtime 或真实用户访谈流程？",
            required_approvals=[_approval("engineering_reviewer", 1)],
        ),
    ]


def _new_item(
    candidate: Mapping[str, Any],
    *,
    dimension: str,
    risk_tier: str,
    question: str,
    required_approvals: list[dict[str, Any]],
) -> dict[str, Any]:
    node_id = candidate["candidate_node_id"]
    return {
        "review_item_id": f"{node_id}.{dimension}",
        "candidate_id": candidate["candidate_id"],
        "candidate_node_id": node_id,
        "dimension": dimension,
        "risk_tier": risk_tier,
        "release_blocking": True,
        "review_question": question,
        "required_approvals": required_approvals,
        "item_status": "pending",
        "reviewer_decisions": [],
        "adjudication": _empty_adjudication(),
    }


def _new_global_item(
    *,
    review_item_id: str,
    dimension: str,
    risk_tier: str,
    question: str,
    required_approvals: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "review_item_id": review_item_id,
        "candidate_id": None,
        "candidate_node_id": None,
        "dimension": dimension,
        "risk_tier": risk_tier,
        "release_blocking": True,
        "review_question": question,
        "required_approvals": required_approvals,
        "item_status": "pending",
        "reviewer_decisions": [],
        "adjudication": _empty_adjudication(),
    }


def _approval(reviewer_role: str, minimum_approvals: int) -> dict[str, Any]:
    return {
        "reviewer_role": reviewer_role,
        "minimum_independent_approvals": minimum_approvals,
    }


def _empty_adjudication() -> dict[str, Any]:
    return {
        "status": "not_required",
        "adjudicator_id": None,
        "decided_on": None,
        "decision": None,
        "rationale": "",
    }


def _initial_summary(
    items: list[Mapping[str, Any]], global_items: list[Mapping[str, Any]]
) -> dict[str, Any]:
    item_ids = [item["review_item_id"] for item in [*global_items, *items]]
    return {
        "total_items": len(item_ids),
        "item_status_counts": {"pending": len(item_ids)},
        "release_blocking_open_item_ids": item_ids,
        "ready_for_compiler": False,
        "candidate_stays_non_published": True,
    }


def _validate_immutable_item_shape(
    item: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    immutable_keys = (
        "review_item_id",
        "candidate_id",
        "candidate_node_id",
        "dimension",
        "risk_tier",
        "release_blocking",
        "review_question",
        "required_approvals",
    )
    for key in immutable_keys:
        if item.get(key) != expected.get(key):
            raise CandidateReviewError(
                f"Review item {expected['review_item_id']} changed immutable field {key}"
            )


def _validate_item_decisions(item: Mapping[str, Any]) -> str:
    decisions = _require_list(item.get("reviewer_decisions"), "reviewer_decisions")
    reviewer_ids: set[str] = set()
    approved_by_role: Counter[str] = Counter()
    has_change = False
    has_conflict = False
    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise CandidateReviewError("Reviewer decision must be an object")
        reviewer_id = _required_text(decision.get("reviewer_id"), "reviewer_id")
        if reviewer_id in reviewer_ids:
            raise CandidateReviewError(
                f"Review item {item['review_item_id']} has duplicate reviewer_id {reviewer_id!r}"
            )
        reviewer_ids.add(reviewer_id)
        role = decision.get("reviewer_role")
        if role not in REVIEWER_ROLES - {"adjudicator"}:
            raise CandidateReviewError(f"Invalid reviewer role: {role!r}")
        decision_value = decision.get("decision")
        if decision_value not in REVIEWER_DECISIONS:
            raise CandidateReviewError(f"Invalid reviewer decision: {decision_value!r}")
        _validate_date(decision.get("reviewed_on"), "reviewed_on")
        _required_text(decision.get("evidence_note"), "evidence_note")
        proposed_change = decision.get("proposed_change")
        if not isinstance(proposed_change, str):
            raise CandidateReviewError("proposed_change must be a string")
        if (
            decision_value in {"approve_with_changes", "reject"}
            and not proposed_change.strip()
        ):
            raise CandidateReviewError(
                f"{decision_value} requires a proposed_change for {item['review_item_id']}"
            )
        if decision_value == "approve":
            approved_by_role[str(role)] += 1
        else:
            has_change = has_change or decision_value in {
                "approve_with_changes",
                "reject",
            }
            has_conflict = has_conflict or decision_value in {
                "reject",
                "needs_adjudication",
            }

    adjudication = item.get("adjudication")
    if not isinstance(adjudication, Mapping):
        raise CandidateReviewError("Review item requires an adjudication object")
    adjudication_status = adjudication.get("status")
    if adjudication_status not in {"not_required", "pending", "resolved"}:
        raise CandidateReviewError("Invalid adjudication status")
    if adjudication_status == "resolved":
        _required_text(adjudication.get("adjudicator_id"), "adjudicator_id")
        _validate_date(adjudication.get("decided_on"), "decided_on")
        if adjudication.get("decision") not in {
            "approve",
            "approve_with_changes",
            "reject",
        }:
            raise CandidateReviewError("Resolved adjudication needs a final decision")
        _required_text(adjudication.get("rationale"), "adjudication.rationale")
        final_adjudication_decision = adjudication["decision"]
    elif any(
        adjudication.get(key) not in (None, "")
        for key in ("adjudicator_id", "decided_on", "decision", "rationale")
    ):
        raise CandidateReviewError("Unresolved adjudication may not contain a decision")

    required = item["required_approvals"]
    if not isinstance(required, list) or not required:
        raise CandidateReviewError("Review item requires required_approvals")
    if adjudication_status == "resolved":
        status = (
            "approved"
            if final_adjudication_decision == "approve"
            else "changes_requested"
        )
    elif has_conflict or adjudication_status == "pending":
        status = "needs_adjudication"
    elif has_change:
        status = "changes_requested"
    elif not decisions:
        status = "pending"
    elif all(
        approved_by_role[approval["reviewer_role"]]
        >= approval["minimum_independent_approvals"]
        for approval in required
    ):
        status = "approved"
    else:
        status = "in_review"
    if item.get("item_status") != status:
        raise CandidateReviewError(
            f"Review item {item['review_item_id']} status is stale; expected {status}"
        )
    return status


def _item_status(item: Mapping[str, Any]) -> str:
    decisions = item["reviewer_decisions"]
    approved_by_role: Counter[str] = Counter(
        decision["reviewer_role"]
        for decision in decisions
        if decision.get("decision") == "approve"
    )
    values = {decision.get("decision") for decision in decisions}
    adjudication = item["adjudication"]
    if adjudication.get("status") == "resolved":
        return (
            "approved"
            if adjudication.get("decision") == "approve"
            else "changes_requested"
        )
    if adjudication.get("status") == "pending" or values & {
        "reject",
        "needs_adjudication",
    }:
        return "needs_adjudication"
    if values & {"approve_with_changes"}:
        return "changes_requested"
    if not decisions:
        return "pending"
    if all(
        approved_by_role[approval["reviewer_role"]]
        >= approval["minimum_independent_approvals"]
        for approval in item["required_approvals"]
    ):
        return "approved"
    return "in_review"


def _render_worklist_item_markdown(item: Mapping[str, Any]) -> list[str]:
    approvals = "；".join(
        f"{approval['reviewer_role']} × {approval['minimum_independent_approvals']}"
        for approval in item["required_approvals"]
    )
    return [
        f"### `{item['review_item_id']}`",
        "",
        f"- 维度：`{item['dimension']}`；风险：`{item['risk_tier']}`；状态：`{item['item_status']}`",
        f"- 审核问题：{item['review_question']}",
        f"- 所需独立批准：{approvals}",
        "- 当前结论：待录入。",
        "",
    ]


def _render_candidate_card(
    candidate: Mapping[str, Any], review_items: list[Mapping[str, Any]]
) -> str:
    node_id = candidate["candidate_node_id"]
    proposal = candidate["proposal"]
    items = "".join(
        _render_item_html(item)
        for item in review_items
        if item["candidate_node_id"] == node_id
    )
    sources = "".join(
        "<li><code>{}</code>：<code>{}</code>；{}</li>".format(
            html.escape(str(source["kind"])),
            html.escape(str(source["locator"])),
            html.escape(str(source["description"])),
        )
        for source in candidate["source_evidence"]["knowledge_source_refs"]
    )
    transitions = "".join(
        "<li><code>{}</code>：<code>{}</code> → <code>{}</code></li>".format(
            html.escape(str(transition["transition_id"])),
            html.escape(_condition_label(transition["when"])),
            html.escape(str(transition["target_node_id"])),
        )
        for transition in proposal["transition_candidates"]
    )
    prompt = html.escape(
        _canonical_prompt(proposal) or "工程控制节点，无用户可见原题。"
    )
    return f"""<section class="candidate">
  <h3>{html.escape(node_id)} <small>({html.escape(str(proposal['node_type']))})</small></h3>
  <p><strong>候选临床意图：</strong>{html.escape(str(proposal['clinical_intent_candidate']))}<br><strong>时间范围：</strong><code>{html.escape(str(proposal['time_window_candidate']))}</code>　<strong>来源置信度：</strong><code>{html.escape(str(candidate['source_confidence']))}</code></p>
  <details open><summary>原题或工程动作</summary><blockquote>{prompt}</blockquote></details>
  <details><summary>来源定位</summary><ul>{sources}</ul></details>
  <details><summary>评分与跳转候选</summary><ul>{transitions}</ul></details>
  <h4>待审项</h4>{items}
</section>"""


def _render_item_html(item: Mapping[str, Any]) -> str:
    approvals = "；".join(
        f"{approval['reviewer_role']} × {approval['minimum_independent_approvals']}"
        for approval in item["required_approvals"]
    )
    return f"""<section class="item {html.escape(str(item['risk_tier']))}">
  <strong><code>{html.escape(str(item['review_item_id']))}</code></strong>
  <p>{html.escape(str(item['review_question']))}</p>
  <p>维度：<code>{html.escape(str(item['dimension']))}</code>　风险：<code>{html.escape(str(item['risk_tier']))}</code>　状态：<code>{html.escape(str(item['item_status']))}</code><br>所需独立批准：{html.escape(approvals)}</p>
</section>"""


def _canonical_prompt(proposal: Mapping[str, Any]) -> str:
    expressions = proposal.get("natural_language_expression_candidates")
    if not isinstance(expressions, list) or not expressions:
        return ""
    first = expressions[0]
    return str(first.get("text", "")) if isinstance(first, Mapping) else ""


def _condition_label(condition: Mapping[str, Any]) -> str:
    if condition.get("always") is True:
        return "default"
    scores = condition.get("score_in")
    if isinstance(scores, list):
        return "score: " + "、".join(str(score) for score in scores)
    return json.dumps(dict(condition), ensure_ascii=False, sort_keys=True)


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateReviewError(f"Missing required non-empty string {name}")
    return value.strip()


def _require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise CandidateReviewError(f"{name} must be a list")
    return value


def _validate_date(value: Any, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 10
        or value[4] != "-"
        or value[7] != "-"
    ):
        raise CandidateReviewError(f"{name} must use YYYY-MM-DD")
    year, month, day = value[:4], value[5:7], value[8:]
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        raise CandidateReviewError(f"{name} must use YYYY-MM-DD")


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _project_relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(_PROJECT_ROOT))
    except ValueError:
        return str(path)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    """Create a new record or render the current G Pilot Phase 5 workspace."""

    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--initialize", action="store_true")
    actions.add_argument("--refresh", action="store_true")
    parser.add_argument("--record", type=Path, default=DEFAULT_REVIEW_RECORD_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_REVIEW_WORKSPACE_DIR)
    arguments = parser.parse_args()
    if arguments.initialize:
        print(initialize_g_pilot_review_record(output_path=arguments.record))
    elif arguments.refresh:
        print(refresh_g_pilot_review_record(review_record_path=arguments.record))
    print(
        build_g_pilot_review_workspace(
            review_record_path=arguments.record, output_dir=arguments.output_dir
        )
    )


if __name__ == "__main__":
    main()
