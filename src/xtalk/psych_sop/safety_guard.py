"""Keyword-based safety guard for the psychology demo."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml


RiskLevel = Literal["none", "low", "moderate", "high"]
RiskType = Literal["none", "self_harm", "harm_to_others", "abuse", "panic", "unknown"]

DEFAULT_SAFETY_RULES_PATH = Path(__file__).with_name("safety_rules.yaml")


@dataclass(slots=True)
class SafetyResult:
    risk_level: RiskLevel = "none"
    risk_type: RiskType = "none"
    matched_signals: list[str] = field(default_factory=list)
    should_interrupt_sop: bool = False


class SafetyGuard:
    """Classify user text with editable keyword rules.

    TODO(safety): replace keyword guard with validated classifier.
    """

    def __init__(self, rules_path: str | Path | None = None) -> None:
        path = Path(rules_path) if rules_path else DEFAULT_SAFETY_RULES_PATH
        self.rules = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.interrupt_levels = set(self.rules.get("interrupt_levels", ["high"]))

    def classify(self, text: str) -> SafetyResult:
        lowered = text.strip().lower()
        if not lowered:
            return SafetyResult()

        best_level: RiskLevel = "none"
        best_type: RiskType = "none"
        matched: list[str] = []
        for level in ("high", "moderate"):
            groups = self.rules.get("risk_levels", {}).get(level, {})
            for risk_type, signals in groups.items():
                hits = [signal for signal in signals if str(signal).lower() in lowered]
                if hits:
                    best_level = level  # type: ignore[assignment]
                    best_type = risk_type  # type: ignore[assignment]
                    matched.extend(hits)
                    return SafetyResult(
                        risk_level=best_level,
                        risk_type=best_type,
                        matched_signals=matched,
                        should_interrupt_sop=best_level in self.interrupt_levels,
                    )
        return SafetyResult()
