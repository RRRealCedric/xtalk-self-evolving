"""Rule-based counseling agent for the text-only SOP demo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .scale_schema import ScaleOption


DEFAULT_PROMPTS_PATH = Path(__file__).with_name("prompts.yaml")


class CounselingAgent:
    """Generate concise Chinese responses from SOP and scale state.

    The first version is rule-based and keeps an LLM replacement point clear.

    TODO(voice): connect CLI flow to X-Talk ASR/TTS pipeline.
    """

    def __init__(self, prompts_path: str | Path | None = None) -> None:
        path = Path(prompts_path) if prompts_path else DEFAULT_PROMPTS_PATH
        self.prompts = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.prompt_version = str(
            self.prompts.get("prompt_version", "psych_sop_demo_v0.1")
        )

    def render(
        self,
        *,
        node_id: str,
        action: str,
        scale_title: str | None = None,
        current_question: str | None = None,
        options: list[ScaleOption] | None = None,
        progress: dict[str, Any] | None = None,
        score: int | None = None,
        interpretation: dict[str, Any] | None = None,
        crisis_response: str = "",
        memory_context: list[dict[str, Any]] | None = None,
        selected_scale: str | None = None,
    ) -> str:
        del memory_context
        if node_id == "START":
            return "你好，我可以带你完成一个结构化心理量表 demo。"
        if node_id == "EXPLAIN_BOUNDARY":
            return str(self.prompts.get("boundary_text", "")).strip()
        if node_id == "GET_CONSENT":
            return "如果你愿意继续，请回复“同意”或“开始”。如果不想继续，也可以直接说“退出”。"
        if node_id == "GOAL_INQUIRY":
            return "你今天主要想了解哪方面的状态呢？比如焦虑、情绪低落，或者只是想测试流程。"
        if node_id == "SCALE_SELECTION":
            default_hint = f"默认将使用 {selected_scale}。" if selected_scale else ""
            return (
                "我们可以先做 GAD-7 或 PHQ-9。"
                "GAD-7 偏向焦虑筛查，PHQ-9 偏向情绪低落筛查。"
                f"{default_hint} 请选择一个，或直接回车使用默认。"
            )
        if node_id == "RISK_CHECK":
            return "开始前确认一下，你现在是否有立即伤害自己、伤害他人，或无法保证安全的风险？"
        if node_id in {"SCALE_LOOP", "CLARIFY_ITEM"}:
            return self._render_scale_item(
                current_question=current_question or "",
                options=options or [],
                progress=progress or {},
                clarify=node_id == "CLARIFY_ITEM" or action == "clarify_item",
                scale_title=scale_title,
            )
        if node_id == "COMPUTE_SCORE":
            return "我已经记录完这些回答，现在计算量表总分。"
        if node_id == "EXPLAIN_RESULT":
            label = (
                interpretation.get("label", "未匹配") if interpretation else "未匹配"
            )
            description = (
                interpretation.get("description", "")
                if interpretation
                else "当前配置没有对应解释。"
            )
            description = str(description).strip()
            if description and description[-1] not in ".。！？!?":
                description = f"{description}。"
            explanation = f"量表解释为：{description}" if description else ""
            return (
                f"这次 {scale_title or '量表'} 的总分是 {score}。"
                f"作为筛查结果，它属于“{label}”范围。{explanation}"
                "这不是诊断，也不能替代专业评估。如果这些感受持续影响生活，建议和专业人士进一步讨论。"
            )
        if node_id == "SUPPORTIVE_CLOSE":
            return "谢谢你完成这个 demo。你可以把结果当作一次自我观察记录，之后也可以继续优化 SOP 或接入语音流程。"
        if node_id == "CRISIS_RESPONSE":
            return crisis_response.strip()
        if node_id == "ABORTED":
            return "好的，我尊重你的决定。我们先停在这里，这次不会继续量表流程。"
        return "我会继续按流程协助你。"

    def _render_scale_item(
        self,
        *,
        current_question: str,
        options: list[ScaleOption],
        progress: dict[str, Any],
        clarify: bool,
        scale_title: str | None,
    ) -> str:
        prefix = ""
        if clarify:
            prefix = "这道题只是在询问最近两周这项感受出现的频率，不是在评价你。"
        option_text = "，".join(
            f"{option.option_id}={option.description}" for option in options
        )
        current = progress.get("current", "?")
        total = progress.get("total", "?")
        hint = str(self.prompts.get("option_hint", ""))
        return (
            f"{prefix}{scale_title or '量表'} 第 {current}/{total} 题："
            f"{current_question} 选项是：{option_text}。{hint}"
        )
