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
MAX_TEMPLATE_JSON_BYTES = 4 * 1024 * 1024
MAX_TEMPLATE_ITEMS = 4096
MAX_PDF_BYTES = 128 * 1024 * 1024
MAX_PDF_PAGES = 1000
MAX_PDF_WIDGETS = 100_000
MAX_PDF_WIDGET_VALUE_CHARS = 16_384


class _SCIDPDFLimitError(ValueError):
    """Internal marker for deterministic PDF resource-limit failures."""


def module_prefix(field_id: str | None) -> str | None:
    """Return the first alphabetic prefix for a SCID field id."""

    if not field_id:
        return None
    match = re.match(r"([A-Za-z]+)", field_id)
    return match.group(1)[0].upper() if match else None


def _scan_field_id(item: dict[str, Any]) -> str:
    qid = _bounded_required_string(item.get("qid"), "qid", 256)
    copy_list = item.get("copy_list")
    if not isinstance(copy_list, list) or len(copy_list) != 1:
        raise ValueError("SCID scan item copy_list must contain exactly one target")
    target = _bounded_required_string(copy_list[0], "copy_list[0]", 256)
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
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"Unable to inspect SCID scan JSON: {path}") from exc
    if file_size > MAX_TEMPLATE_JSON_BYTES:
        raise ValueError(f"SCID scan JSON exceeds {MAX_TEMPLATE_JSON_BYTES} bytes")
    try:
        with path.open("rb") as handle:
            raw_bytes = handle.read(MAX_TEMPLATE_JSON_BYTES + 1)
        if len(raw_bytes) > MAX_TEMPLATE_JSON_BYTES:
            raise ValueError(f"SCID scan JSON exceeds {MAX_TEMPLATE_JSON_BYTES} bytes")
        raw_text = raw_bytes.decode("utf-8")
        raw_items = json.loads(
            raw_text,
            parse_constant=_raise_invalid_json_constant,
        )
    except ValueError as exc:
        if str(exc).startswith("SCID scan JSON exceeds"):
            raise
        raise ValueError(f"Unable to parse SCID scan JSON: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to parse SCID scan JSON: {path}") from exc
    if not isinstance(raw_items, list):
        raise ValueError("SCID scan JSON must be a list of questions")
    if not raw_items:
        raise ValueError("SCID scan JSON must contain at least one question")
    if len(raw_items) > MAX_TEMPLATE_ITEMS:
        raise ValueError(
            f"SCID scan JSON contains more than {MAX_TEMPLATE_ITEMS} questions"
        )

    priority = _normalize_priority_modules(priority_modules)
    fields: dict[str, SCIDField] = {}
    scan_order: list[str] = []
    module_entries: dict[str, SCIDField] = {}
    scan_field_by_target: dict[str, str] = {}

    seen_targets: set[str] = set()
    available_modules: set[str] = set()
    validated_items: list[tuple[dict[str, Any], str, str, str]] = []
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise ValueError(f"SCID scan item {index} must be an object")
        item = dict(raw_item)
        _validate_scan_item(item, index=index)
        field_id = _scan_field_id(item)
        qid = _bounded_required_string(item["qid"], f"item[{index}].qid", 256)
        target = _bounded_required_string(
            item["copy_list"][0],
            f"item[{index}].copy_list[0]",
            256,
        )
        if field_id in fields or any(
            existing[1] == field_id for existing in validated_items
        ):
            raise ValueError(f"Duplicate SCID field_id: {field_id}")
        if target in seen_targets:
            raise ValueError(f"Duplicate SCID target_field_id: {target}")
        prefix = module_prefix(target)
        if prefix is None:
            raise ValueError(f"SCID target has no module prefix: {target!r}")
        seen_targets.add(target)
        available_modules.add(prefix)
        validated_items.append((item, field_id, qid, target))

    missing_priority = sorted(set(priority) - available_modules)
    if missing_priority:
        raise ValueError(f"Priority modules have no scan targets: {missing_priority}")
    scan_ids = {field_id for _, field_id, _, _ in validated_items}
    colliding_targets = sorted(scan_ids & seen_targets)
    if colliding_targets:
        raise ValueError(
            f"Module targets collide with scan field ids: {colliding_targets}"
        )

    for item, field_id, qid, target in validated_items:
        prefix = module_prefix(target)
        field = SCIDField(
            field_id=field_id,
            question_text=_bounded_required_string(
                item["text"],
                f"{field_id}.text",
                16_384,
            ),
            section="scan",
            source_qid=qid,
            target_field_id=target,
            module=prefix,
            kind="scan",
            tag=_bounded_optional_string(
                item.get("tag", ""),
                f"{field_id}.tag",
                4096,
            ),
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
    _validate_template_graph(template)
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

        pdf_path = Path(path)
        try:
            file_size = pdf_path.stat().st_size
        except OSError as exc:
            raise ValueError(f"Unable to inspect SCID PDF: {pdf_path}") from exc
        if file_size > MAX_PDF_BYTES:
            raise ValueError(f"SCID PDF exceeds {MAX_PDF_BYTES} bytes")

        values: list[PDFWidgetValue] = []
        widget_count = 0
        try:
            with fitz.open(str(pdf_path)) as doc:
                if type(doc.page_count) is not int or doc.page_count < 0:
                    raise _SCIDPDFLimitError("SCID PDF has an invalid page count")
                if doc.page_count > MAX_PDF_PAGES:
                    raise _SCIDPDFLimitError(
                        f"SCID PDF contains more than {MAX_PDF_PAGES} pages"
                    )
                for page_index, page in enumerate(doc):
                    for widget in page.widgets() or ():
                        widget_count += 1
                        if widget_count > MAX_PDF_WIDGETS:
                            raise _SCIDPDFLimitError(
                                f"SCID PDF contains more than {MAX_PDF_WIDGETS} widgets"
                            )
                        raw_value = widget.field_value
                        if raw_value in (None, "", "Off"):
                            continue
                        value = str(raw_value)
                        if len(value) > MAX_PDF_WIDGET_VALUE_CHARS:
                            raise _SCIDPDFLimitError(
                                "SCID PDF widget value exceeds "
                                f"{MAX_PDF_WIDGET_VALUE_CHARS} characters"
                            )
                        values.append(
                            PDFWidgetValue(
                                page_number=page_index + 1,
                                field_name=_bounded_optional_string(
                                    str(widget.field_name or ""),
                                    "PDF widget field_name",
                                    4096,
                                ),
                                field_type=_bounded_optional_string(
                                    str(widget.field_type_string or ""),
                                    "PDF widget field_type",
                                    256,
                                ),
                                value=value,
                            )
                        )
        except _SCIDPDFLimitError:
            raise
        except Exception as exc:
            raise ValueError(f"Unable to parse SCID PDF: {pdf_path}") from exc
        return values

    @staticmethod
    def field_names_from_values(values: Iterable[PDFWidgetValue]) -> set[str]:
        """Return field names from extracted widget values."""

        return {item.field_name for item in values}


def _validate_scan_item(item: dict[str, Any], *, index: int) -> None:
    required = {"qid", "text", "copy_list"}
    allowed = required | {"tag", "jump_table"}
    missing = sorted(required - item.keys())
    if missing:
        raise ValueError(f"SCID scan item {index} is missing keys: {missing}")
    unknown = sorted(item.keys() - allowed)
    if unknown:
        raise ValueError(f"SCID scan item {index} contains unknown keys: {unknown}")
    _bounded_required_string(item["qid"], f"item[{index}].qid", 256)
    _bounded_required_string(item["text"], f"item[{index}].text", 16_384)
    if "tag" in item:
        _bounded_optional_string(item["tag"], f"item[{index}].tag", 4096)
    if "jump_table" in item:
        if not isinstance(item["jump_table"], dict):
            raise ValueError(f"item[{index}].jump_table must be an object")
        if item["jump_table"]:
            raise ValueError(
                f"item[{index}].jump_table is unsupported by the scan graph"
            )
    _scan_field_id(item)


def _normalize_priority_modules(priority_modules: Iterable[str]) -> tuple[str, ...]:
    if isinstance(priority_modules, (str, bytes)):
        raise ValueError("priority_modules must be an iterable of module strings")
    normalized: list[str] = []
    for index, value in enumerate(priority_modules):
        if not isinstance(value, str):
            raise ValueError(f"priority_modules[{index}] must be a string")
        module = value.strip().upper()
        if len(module) != 1 or not module.isalpha() or not module.isascii():
            raise ValueError(f"priority_modules[{index}] must be one ASCII letter")
        if module in normalized:
            raise ValueError(f"Duplicate priority module: {module}")
        normalized.append(module)
    return tuple(normalized)


def _raise_invalid_json_constant(token: str) -> None:
    raise ValueError(f"Invalid JSON number: {token}")


def _validate_template_graph(template: SCIDTemplate) -> None:
    if len(template.scan_order) != len(set(template.scan_order)):
        raise ValueError("SCID scan_order contains duplicate fields")
    if any(field_id not in template.fields for field_id in template.scan_order):
        raise ValueError("SCID scan_order references an unknown field")
    for target, scan_field_id in template.scan_field_by_target.items():
        if scan_field_id not in template.scan_order:
            raise ValueError(f"SCID target {target!r} references a non-scan field")
        field = template.fields[scan_field_id]
        if field.target_field_id != target:
            raise ValueError(f"SCID target mapping is inconsistent for {target!r}")
        if (
            module_prefix(target) in template.priority_modules
            and target not in template.fields
        ):
            raise ValueError(f"Priority module target is not runnable: {target!r}")


def _bounded_required_string(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    parsed = value.strip()
    if not parsed:
        raise ValueError(f"{field_name} must not be empty")
    if len(parsed) > maximum:
        raise ValueError(f"{field_name} exceeds {maximum} characters")
    return parsed


def _bounded_optional_string(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    parsed = value.strip()
    if len(parsed) > maximum:
        raise ValueError(f"{field_name} exceeds {maximum} characters")
    return parsed
