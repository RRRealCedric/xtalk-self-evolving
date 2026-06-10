"""Deterministic execution engine for structured psychology scales."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .scale_loader import ScaleLoader
from .scale_schema import ScaleOption, ScaleSessionState, ScaleSpec


class ScaleEngine:
    """Run a structured scale without relying on LLM state."""

    def __init__(self, loader: ScaleLoader | None = None) -> None:
        self.loader = loader or ScaleLoader()
        self.scale: ScaleSpec | None = None
        self.state: ScaleSessionState | None = None

    def load_scale(self, scale_id: str) -> ScaleSpec:
        self.scale = self.loader.load_scale(scale_id)
        return self.scale

    def start_scale(self, scale_id: str) -> ScaleSessionState:
        self.scale = self.loader.load_scale(scale_id)
        if not self.scale.questions:
            raise ValueError(f"Scale {scale_id} has no runnable questions")
        self.state = ScaleSessionState(
            scale_id=self.scale.scale_id, status="in_progress"
        )
        return self.state

    def get_current_question(self) -> str:
        self._require_running()
        assert self.scale is not None and self.state is not None
        return self.scale.questions[self.state.current_index]

    def get_options(self) -> list[ScaleOption]:
        self._require_scale()
        assert self.scale is not None
        return list(self.scale.options)

    def record_answer(
        self,
        question_index: int,
        option_id: int,
        raw_user_text: str,
        confidence: float = 1.0,
    ) -> None:
        del confidence
        self._require_running()
        assert self.scale is not None and self.state is not None
        option_ids = {option.option_id for option in self.scale.options}
        if option_id not in option_ids:
            raise ValueError(f"Invalid option id {option_id}")
        if question_index < 0 or question_index >= len(self.scale.questions):
            raise IndexError(question_index)
        self.state.answers[question_index] = option_id
        self.state.raw_answers[question_index] = raw_user_text
        if question_index in self.state.skipped:
            self.state.skipped.remove(question_index)
        self.state.touch()

    def skip_question(self, question_index: int) -> None:
        self._require_running()
        assert self.scale is not None and self.state is not None
        if question_index not in self.state.skipped:
            self.state.skipped.append(question_index)
        self.state.raw_answers[question_index] = "skipped"
        self.state.touch()

    def has_next_question(self) -> bool:
        self._require_running()
        assert self.scale is not None and self.state is not None
        return self.state.current_index + 1 < len(self.scale.questions)

    def next_question(self) -> bool:
        self._require_running()
        assert self.state is not None
        if not self.has_next_question():
            return False
        self.state.current_index += 1
        self.state.touch()
        return True

    def compute_score(self) -> int:
        self._require_scale()
        assert self.scale is not None and self.state is not None
        options_by_id = {option.option_id: option for option in self.scale.options}
        score = 0
        for option_id in self.state.answers.values():
            score += options_by_id[option_id].score
        self.state.status = "completed"
        self.state.touch()
        return score

    def get_score_interpretation(self, score: int) -> dict[str, Any]:
        self._require_scale()
        assert self.scale is not None
        interpretation = self.scale.score_interpretation
        if isinstance(interpretation, list):
            for item in interpretation:
                if not isinstance(item, dict):
                    continue
                lower = int(item.get("min", item.get("from", -(10**9))))
                upper = int(item.get("max", item.get("to", 10**9)))
                if lower <= score <= upper:
                    return dict(item)
        if isinstance(interpretation, dict):
            for key, value in interpretation.items():
                if isinstance(value, dict):
                    lower = int(value.get("min", value.get("from", -(10**9))))
                    upper = int(value.get("max", value.get("to", 10**9)))
                    if lower <= score <= upper:
                        result = dict(value)
                        result.setdefault("label", str(key))
                        return result
                if "-" in str(key):
                    left, right = str(key).split("-", 1)
                    if int(left) <= score <= int(right):
                        return {"label": str(key), "description": str(value)}
        return {
            "label": "未匹配",
            "description": "当前量表配置没有提供对应分数段解释。",
        }

    def get_progress(self) -> dict[str, int | str]:
        self._require_running()
        assert self.scale is not None and self.state is not None
        total = len(self.scale.questions)
        answered = len(self.state.answers)
        return {
            "scale_id": self.state.scale_id,
            "current": min(self.state.current_index + 1, total),
            "total": total,
            "answered": answered,
            "skipped": len(self.state.skipped),
            "status": self.state.status,
        }

    def abort_scale(self, reason: str) -> None:
        self._require_running()
        assert self.state is not None
        self.state.status = "aborted"
        self.state.abort_reason = reason
        self.state.touch()

    def parse_answer(self, user_text: str) -> tuple[int | None, float, str]:
        """Map user text to an option id with a coarse confidence."""

        self._require_scale()
        assert self.scale is not None
        text = user_text.strip().lower()
        for option in self.scale.options:
            if text == str(option.option_id):
                return option.option_id, 1.0, "matched_option_id"
        for option in self.scale.options:
            if option.description and option.description.lower() in text:
                return option.option_id, 0.95, "matched_option_text"

        keyword_map = {
            0: ["没有", "无", "从不", "完全没有", "not at all", "never"],
            1: ["有一点", "一点", "几天", "偶尔", "轻微", "some days"],
            2: ["一半", "超过一半", "经常", "不少", "more than half"],
            3: ["每天", "几乎每天", "总是", "非常", "严重", "almost every day"],
        }
        for option_id, keywords in keyword_map.items():
            if any(keyword in text for keyword in keywords):
                return option_id, 0.7, "matched_keyword"
        return None, 0.0, "unrecognized"

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable state snapshot."""

        return asdict(self.state) if self.state else {}

    def _require_scale(self) -> None:
        if self.scale is None:
            raise RuntimeError("No scale loaded")

    def _require_running(self) -> None:
        self._require_scale()
        if self.state is None:
            raise RuntimeError("No scale session started")
