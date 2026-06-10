"""SOP data structures for the psychology demo."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SOPNode:
    id: str
    goal: str
    allowed_actions: list[str] = field(default_factory=list)
    transitions: list[dict[str, str]] = field(default_factory=list)
    prompt_hints: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SOPSpec:
    sop_id: str
    global_rules: list[str] = field(default_factory=list)
    crisis_response: str = ""
    nodes: dict[str, SOPNode] = field(default_factory=dict)


@dataclass(slots=True)
class NextAction:
    node_id: str
    action: str
    reason: str = ""
    should_end: bool = False
    data: dict[str, Any] = field(default_factory=dict)
