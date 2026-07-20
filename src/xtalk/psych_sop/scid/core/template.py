"""SCID template loading and lightweight PDF widget inspection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .schema import SCIDField, SCIDTemplate


PACKAGE_PYSC_DIR = Path(__file__).resolve().parents[2] / "data" / "pysc"
DEFAULT_SCID_SCAN_PATH = PACKAGE_PYSC_DIR / "SCID-5-S.json"
DEFAULT_PRIORITY_MODULES = ("F", "G", "K")


def module_prefix(field_id: str | None) -> str | None:
    """Return the first alphabetic prefix for a SCID field id."""

    if not field_id:
        return None
    match = re.match(r"([A-Za-z]+)", field_id)
    return match.group(1)[0].upper() if match else None


def _scan_field_id(item: dict[str, Any]) -> str:
    qid = str(item.get("qid") or "").strip()
    copy_list = item.get("copy_list") or []
    target = str(copy_list[0]).strip() if copy_list else ""
    if not qid or not target:
        raise ValueError(f"Invalid SCID scan item: {item!r}")
    return f"{qid}-{target}"


def load_scid_template(
    *,
    scan_json_path: str | Path | None = None,
    priority_modules: Iterable[str] = DEFAULT_PRIORITY_MODULES,
) -> SCIDTemplate:
    """Load the phase-one runnable SCID template.

    Phase one runs the official scan items from ``SCID-5-S.json`` and adds a
    small module-entry placeholder for priority modules. Those placeholders keep
    the voice system usable while the full 346-page SCID graph is incrementally
    converted into structured module questions.
    """

    path = Path(scan_json_path) if scan_json_path else DEFAULT_SCID_SCAN_PATH
    raw_items = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_items, list):
        raise ValueError("SCID scan JSON must be a list of questions")

    priority = tuple(prefix.upper()[0] for prefix in priority_modules)
    fields: dict[str, SCIDField] = {}
    scan_order: list[str] = []
    module_entries: dict[str, SCIDField] = {}
    scan_field_by_target: dict[str, str] = {}

    for item in raw_items:
        field_id = _scan_field_id(item)
        qid, target = field_id.split("-", 1)
        prefix = module_prefix(target)
        field = SCIDField(
            field_id=field_id,
            question_text=str(item.get("text") or "").strip(),
            section="scan",
            source_qid=qid,
            target_field_id=target,
            module=prefix,
            kind="scan",
            tag=str(item.get("tag") or "").strip(),
            latency_mode="optimistic_scan",
        )
        fields[field_id] = field
        scan_order.append(field_id)
        scan_field_by_target[target] = field_id

        if prefix in priority and target not in module_entries:
            module_entries[target] = _build_module_entry_field(
                target=target,
                source_field=field,
            )

    fields.update(module_entries)
    template = SCIDTemplate(
        template_id="scid_phase1_scan_fgk_v1",
        fields=fields,
        scan_order=scan_order,
        priority_modules=priority,
        scan_field_by_target=scan_field_by_target,
    )
    return template


def _build_module_entry_field(*, target: str, source_field: SCIDField) -> SCIDField:
    prefix = module_prefix(target)
    module_name = {
        "F": "焦虑相关模块",
        "G": "强迫及相关模块",
        "K": "外化或注意力相关模块",
    }.get(prefix or "", f"{prefix or target} 模块")
    return SCIDField(
        field_id=target,
        question_text=(
            f"刚才的扫描题提示可能需要进一步了解{module_name}。"
            "请你结合自己的经历，具体说说这个问题出现的情境、频率、持续时间，"
            "以及它是否影响生活或工作。"
        ),
        section="priority_module",
        source_qid=source_field.source_qid,
        target_field_id=None,
        module=prefix,
        kind="module_entry",
        tag=f"{module_name}入口：由{source_field.field_id}触发",
        latency_mode="cautious_module",
    )


@dataclass(slots=True)
class PDFWidgetValue:
    """One non-empty PDF form widget value."""

    page_number: int
    field_name: str
    field_type: str
    value: str


class SCIDPDFWidgetExtractor:
    """Inspect AcroForm widgets from SCID PDFs.

    This is intentionally read-only. It is used for fixtures, smoke checks, and
    future gold-label extraction; runtime phase one does not require a PDF.
    """

    @staticmethod
    def extract_nonempty(path: str | Path) -> list[PDFWidgetValue]:
        """Extract non-empty widget values from a PDF via PyMuPDF."""

        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("PyMuPDF/fitz is required to inspect SCID PDFs") from exc

        doc = fitz.open(str(path))
        values: list[PDFWidgetValue] = []
        for page_index, page in enumerate(doc):
            widgets = list(page.widgets() or [])
            for widget in widgets:
                value = widget.field_value
                if value in (None, "", "Off"):
                    continue
                values.append(
                    PDFWidgetValue(
                        page_number=page_index + 1,
                        field_name=str(widget.field_name),
                        field_type=str(widget.field_type_string),
                        value=str(value),
                    )
                )
        return values

    @staticmethod
    def field_names_from_values(values: Iterable[PDFWidgetValue]) -> set[str]:
        """Return field names from extracted widget values."""

        return {item.field_name for item in values}
