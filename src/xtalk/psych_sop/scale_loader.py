"""Load psychology scale JSON specs with built-in demo fallbacks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .scale_schema import ScaleOption, ScaleSpec


EXTERNAL_SCALE_DIR = Path(__file__).resolve().parents[4] / "psydata" / "pysc"
PACKAGE_SCALE_DIR = Path(__file__).resolve().parent / "data" / "pysc"
DEFAULT_SCALE_DIR = EXTERNAL_SCALE_DIR


def _standard_options() -> list[dict[str, Any]]:
    return [
        {"id": 0, "description": "完全没有", "score": 0},
        {"id": 1, "description": "有几天", "score": 1},
        {"id": 2, "description": "超过一半天数", "score": 2},
        {"id": 3, "description": "几乎每天", "score": 3},
    ]


BUILTIN_SCALES: dict[str, dict[str, Any]] = {
    "GAD-7": {
        "title": "GAD-7 广泛性焦虑量表",
        "description": "用于筛查最近两周焦虑相关困扰程度的自评量表。",
        "introductions": {
            "zh": "下面七个问题询问最近两周的感受。请选择最接近你情况的选项。"
        },
        "score_interpretation": [
            {
                "min": 0,
                "max": 4,
                "label": "较低",
                "description": "结果可能提示焦虑困扰较低。",
            },
            {
                "min": 5,
                "max": 9,
                "label": "轻度",
                "description": "结果可能提示轻度焦虑相关困扰。",
            },
            {
                "min": 10,
                "max": 14,
                "label": "中度",
                "description": "结果可能提示中度焦虑相关困扰。",
            },
            {
                "min": 15,
                "max": 21,
                "label": "较高",
                "description": "结果可能提示较高焦虑相关困扰。",
            },
        ],
        "questions": [
            "感到紧张、焦虑或急切。",
            "不能停止或控制担忧。",
            "对各种各样的事情担忧过多。",
            "很难放松下来。",
            "由于不安而无法静坐。",
            "变得容易烦恼或急躁。",
            "感到好像有什么可怕的事情会发生。",
        ],
        "options": _standard_options(),
        "additional_questions": None,
    },
    "PHQ-9": {
        "title": "PHQ-9 患者健康问卷",
        "description": "用于筛查最近两周抑郁相关困扰程度的自评量表。",
        "introductions": {
            "zh": "下面九个问题询问最近两周的感受。请选择最接近你情况的选项。"
        },
        "score_interpretation": [
            {
                "min": 0,
                "max": 4,
                "label": "较低",
                "description": "结果可能提示抑郁相关困扰较低。",
            },
            {
                "min": 5,
                "max": 9,
                "label": "轻度",
                "description": "结果可能提示轻度抑郁相关困扰。",
            },
            {
                "min": 10,
                "max": 14,
                "label": "中度",
                "description": "结果可能提示中度抑郁相关困扰。",
            },
            {
                "min": 15,
                "max": 19,
                "label": "中重度",
                "description": "结果可能提示中重度抑郁相关困扰。",
            },
            {
                "min": 20,
                "max": 27,
                "label": "较高",
                "description": "结果可能提示较高抑郁相关困扰。",
            },
        ],
        "questions": [
            "做事时提不起劲或没有兴趣。",
            "感到心情低落、沮丧或绝望。",
            "入睡困难、睡不安稳，或睡眠过多。",
            "感到疲倦或没有活力。",
            "食欲不振或吃太多。",
            "觉得自己很糟，或觉得自己让自己或家人失望。",
            "对事物专注有困难，例如阅读报纸或看电视。",
            "动作或说话速度慢到别人可能已经察觉，或相反地坐立不安。",
            "想到自己不如死了算了，或以某种方式伤害自己。",
        ],
        "options": _standard_options(),
        "additional_questions": None,
    },
    "SCL-90": {
        "title": "SCL-90 症状自评量表",
        "description": "多维度症状自评量表。本 demo 第一版只支持加载占位，不执行完整流程。",
        "introductions": {"zh": "SCL-90 已预留加载接口。"},
        "score_interpretation": {},
        "questions": [],
        "options": [],
        "additional_questions": None,
    },
}


class ScaleLoader:
    """Load and normalize scale JSON files.

    If a JSON file is a Git LFS pointer or unavailable, the loader falls back to
    built-in PHQ-9/GAD-7 demo specs so the CLI remains runnable.
    """

    def __init__(self, scale_dir: str | Path | None = None) -> None:
        self.scale_dirs = (
            [Path(scale_dir)] if scale_dir else [EXTERNAL_SCALE_DIR, PACKAGE_SCALE_DIR]
        )

    def load_scale(self, scale_id: str) -> ScaleSpec:
        """Load one scale by id."""

        normalized_id = self._normalize_scale_id(scale_id)
        raw = self._load_json_file(normalized_id)
        if raw is None:
            raw = BUILTIN_SCALES.get(normalized_id)
        if raw is None:
            raise ValueError(f"Unknown scale id: {scale_id}")
        return self._normalize_spec(normalized_id, raw)

    def available_scales(self) -> list[str]:
        """Return scale ids known to the demo."""

        return sorted(BUILTIN_SCALES)

    def _load_json_file(self, scale_id: str) -> dict[str, Any] | None:
        path = self._resolve_scale_path(scale_id)
        if path is None:
            return None
        text = path.read_text(encoding="utf-8").strip()
        if text.startswith("version https://git-lfs.github.com/spec/v1"):
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        data["_source_path"] = str(path)
        return data

    def _resolve_scale_path(self, scale_id: str) -> Path | None:
        for scale_dir in self.scale_dirs:
            path = scale_dir / f"{scale_id}.json"
            if path.exists():
                return path
        return None

    @staticmethod
    def _normalize_scale_id(scale_id: str) -> str:
        value = scale_id.strip().upper().replace("_", "-")
        aliases = {
            "GAD7": "GAD-7",
            "PHQ9": "PHQ-9",
            "SCL90": "SCL-90",
        }
        return aliases.get(value, value)

    def _normalize_spec(self, scale_id: str, raw: dict[str, Any]) -> ScaleSpec:
        options = [
            self._normalize_option(item, index)
            for index, item in enumerate(raw.get("options", []))
        ]
        questions = [
            str(item).strip() for item in raw.get("questions", []) if str(item).strip()
        ]
        return ScaleSpec(
            scale_id=scale_id,
            title=str(raw.get("title") or scale_id),
            description=str(raw.get("description") or ""),
            introductions=(
                raw.get("introductions")
                if isinstance(raw.get("introductions"), dict)
                else {}
            ),
            score_interpretation=raw.get("score_interpretation") or {},
            questions=questions,
            options=options,
            additional_questions=raw.get("additional_questions"),
            source_path=raw.get("_source_path"),
        )

    @staticmethod
    def _normalize_option(raw: dict[str, Any], index: int) -> ScaleOption:
        option_id = raw.get(
            "id",
            raw.get("option_id", raw.get("number", raw.get("编号", index))),
        )
        description = raw.get(
            "description",
            raw.get("label", raw.get("text", raw.get("描述", ""))),
        )
        score = raw.get("score", raw.get("value", raw.get("分值", option_id)))
        return ScaleOption(
            option_id=int(option_id),
            description=str(description),
            score=int(score),
        )
