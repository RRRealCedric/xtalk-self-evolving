"""Frontend dialogue rendering for the SCID voice runtime."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from langchain.chat_models.base import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from .schema import DialogueDirective


class DialogueModel(ABC):
    """Convert safe SCID directives into user-facing speech text."""

    @abstractmethod
    async def render(self, directive: DialogueDirective) -> str:
        """Render one directive."""


class RuleBasedDialogueModel(DialogueModel):
    """Deterministic fallback renderer."""

    async def render(self, directive: DialogueDirective) -> str:
        if directive.directive_type == "partial_ack":
            return directive.question_text or "嗯，我在听，你可以继续说。"
        if directive.directive_type in {"meta_answer", "question_clarification"}:
            return directive.question_text
        if directive.directive_type == "free_chat":
            return "嗯，我听到了。你可以接着说。"
        if directive.directive_type == "resume_prompt":
            return f"好，我们继续。{directive.question_text}"
        if directive.directive_type == "crisis":
            return (
                "我听到你可能正处在危险或非常痛苦的状态。这个系统不能处理紧急危机。"
                "请你现在优先保证安全，尽快联系当地紧急服务、身边可信任的人，"
                "或者专业机构。"
            )
        if directive.directive_type == "complete":
            return "本阶段的 SCID 访谈已经完成了。谢谢你的配合，我们先停在这里。"
        progress = f"{directive.progress_text}。" if directive.progress_text else ""
        return f"{progress}{directive.question_text}"


class SmallLMDialogueModel(DialogueModel):
    """Use the configured small/frontstage chat model for natural wording."""

    SYSTEM_PROMPT = """你是 SCID 语音模式的前台对话助手，只负责把后台 directive 转成自然、简短、温和的口语。
你不能评分，不能推断诊断，不能透露或发明表单分数、字段、JSON 或内部流程。

不同 directive_type 的规则：
ask/clarify/resume_prompt：只问或重问 directive 指定的问题，可以稍微缓和语气，但不要改变题意。
meta_answer/question_clarification/partial_ack：只把 question_text 变成自然口语，不添加诊断信息。
free_chat：只根据 question_text 里的用户原话做普通、简短回应；不要提 SCID、量表、评估流程、分数或诊断。
crisis/complete：按 directive 给出的安全或结束意图回应。

输出必须适合 TTS 朗读，避免项目符号、表格、JSON、括号和长句。"""

    def __init__(self, model: BaseChatModel) -> None:
        self.model = model
        self.fallback = RuleBasedDialogueModel()

    async def render(self, directive: DialogueDirective) -> str:
        try:
            response = await self.model.ainvoke(
                [
                    SystemMessage(content=self.SYSTEM_PROMPT),
                    HumanMessage(
                        content=(
                            "请把以下 directive 转成一句或两句自然口语。"
                            "严格遵守 directive_type 对应的边界，不要评分，不要诊断。\n"
                            f"{directive.snapshot()}"
                        )
                    ),
                ]
            )
            text = str(response.content or "").strip()
            return text or await self.fallback.render(directive)
        except Exception:
            return await self.fallback.render(directive)


def dialogue_model_from_pipeline_agent(agent: Any) -> DialogueModel:
    """Create a frontend dialogue model from the existing X-Talk agent."""

    model = getattr(agent, "model", None)
    if isinstance(model, BaseChatModel):
        return SmallLMDialogueModel(model)
    return RuleBasedDialogueModel()


def frontend_tool_names() -> tuple[str, ...]:
    """Return the allowed frontend conceptual tools.

    The phase-one manager exposes these as an explicit policy surface; it does
    not grant any scoring/write tools to the frontend model.
    """

    return (
        "get_scid_state",
        "get_current_directive",
        "route_user_intent",
        "submit_patient_reply",
        "request_clarification_style",
    )
