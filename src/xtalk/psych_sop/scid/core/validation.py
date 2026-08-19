"""Strict boundary validation shared by SCID model adapters.

The helpers in this module intentionally reject implicit coercions.  Model and
configuration payloads are untrusted boundaries: a JSON string such as
``"false"`` is not a boolean, and non-finite IEEE-754 values are never valid
confidence scores.
"""

from __future__ import annotations

import json
import math
from typing import Any


MAX_MODEL_JSON_BYTES = 64 * 1024
MAX_JSON_ARRAY_ITEMS = 64


def strict_bool(value: Any, *, field_name: str) -> bool:
    """Return a real JSON boolean without applying truthiness coercion."""

    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a boolean")
    return value


def strict_nonnegative_int(
    value: Any,
    *,
    field_name: str,
    maximum: int | None = None,
) -> int:
    """Return a non-negative integer, rejecting booleans and numeric strings."""

    if type(value) is not int:
        raise ValueError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} must be at most {maximum}")
    return value


def strict_finite_float(
    value: Any,
    *,
    field_name: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Return a finite JSON number within optional inclusive bounds."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be finite")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{field_name} must be at most {maximum}")
    return parsed


def strict_string(
    value: Any,
    *,
    field_name: str,
    maximum_length: int,
    allow_empty: bool = False,
    strip: bool = True,
) -> str:
    """Return a bounded string without stringifying arbitrary JSON values."""

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    parsed = value.strip() if strip else value
    if not allow_empty and not parsed:
        raise ValueError(f"{field_name} must not be empty")
    if len(parsed) > maximum_length:
        raise ValueError(
            f"{field_name} must contain at most {maximum_length} characters"
        )
    return parsed


def strict_string_list(
    value: Any,
    *,
    field_name: str,
    maximum_items: int = MAX_JSON_ARRAY_ITEMS,
    maximum_item_length: int = 4096,
    allow_empty_items: bool = False,
) -> list[str]:
    """Return a bounded list containing only bounded strings."""

    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array of strings")
    if len(value) > maximum_items:
        raise ValueError(f"{field_name} must contain at most {maximum_items} items")
    return [
        strict_string(
            item,
            field_name=f"{field_name}[{index}]",
            maximum_length=maximum_item_length,
            allow_empty=allow_empty_items,
        )
        for index, item in enumerate(value)
    ]


def strict_json_loads(text: str, *, maximum_bytes: int = MAX_MODEL_JSON_BYTES) -> Any:
    """Decode bounded JSON while rejecting NaN/Infinity and oversized arrays."""

    if not isinstance(text, str):
        raise ValueError("model output must be text")
    if len(text.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"model output exceeds {maximum_bytes} bytes")

    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {token}")

    value = json.loads(text, parse_constant=reject_constant)
    validate_json_shape(value)
    return value


def validate_json_shape(value: Any, *, path: str = "$") -> None:
    """Reject oversized arrays and non-finite numbers anywhere in decoded JSON."""

    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        if len(value) > MAX_JSON_ARRAY_ITEMS:
            raise ValueError(
                f"{path} contains more than {MAX_JSON_ARRAY_ITEMS} array items"
            )
        for index, item in enumerate(value):
            validate_json_shape(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            validate_json_shape(item, path=f"{path}.{key}")
        return
    raise ValueError(f"{path} contains a non-JSON value")
