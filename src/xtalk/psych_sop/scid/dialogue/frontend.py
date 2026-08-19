"""Frontend dialogue rendering for the SCID voice runtime."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import fields, is_dataclass
from typing import Any, AsyncIterator

from langchain.chat_models.base import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ..core.schema import DialogueDirective


logger = logging.getLogger(__name__)

_MAX_CONTEXT_DEPTH = 4
_MAX_CONTEXT_ITEMS = 64
_MAX_CONTEXT_COLLECTION_ITEMS = 16
_MAX_CONTEXT_STRING_CHARS = 2_048
_MAX_CONTEXT_TOTAL_CHARS = 8_192
_SAFE_CONTEXT_KEYS = frozenset(
    {
        "allowed",
        "allowed_actions",
        "assistant_text",
        "current_question",
        "directive",
        "directive_type",
        "evidence_slot",
        "forbidden",
        "foreground_action",
        "instruction",
        "interaction_mode",
        "interaction_seq",
        "kind",
        "priority",
        "progress_text",
        "provisional",
        "question_text",
        "recent_turns",
        "safe_note",
        "safety_note",
        "source",
        "speculative",
        "terminal",
        "user_text",
    }
)
_OMIT = object()


class FrontendStreamInterrupted(RuntimeError):
    """Signal a frontend model failure after user-visible output began."""

    def __init__(self, *, error_type: str, error_message: str) -> None:
        self.error_type = error_type
        self.error_message = error_message
        self.truncated = True
        super().__init__(f"{error_type}: {error_message}")

    @classmethod
    def from_exception(
        cls,
        exc: Exception,
    ) -> FrontendStreamInterrupted:
        """Build a bounded error without retaining prompts or user text."""

        return cls(
            error_type=type(exc).__name__,
            error_message="frontend model stream interrupted after output",
        )


class DialogueModel(ABC):
    """Convert safe SCID directives into user-facing speech text."""

    @abstractmethod
    async def render(self, directive: DialogueDirective) -> str:
        """Render one directive."""

    async def stream(
        self,
        directive: DialogueDirective,
        context: Any | None = None,
    ) -> AsyncIterator[str]:
        """Stream one directive. Fallback models may yield a single chunk."""

        del context
        text = await self.render(directive)
        if text:
            yield text

    async def render_cache_candidate(
        self, directive: DialogueDirective
    ) -> tuple[str, bool]:
        """Render cacheable wording and report whether generation succeeded."""

        text = str(await self.render(directive) or "")
        return text, bool(text.strip())


class RuleBasedDialogueModel(DialogueModel):
    """Deterministic fallback renderer."""

    async def render(self, directive: DialogueDirective) -> str:
        """Render a directive with deterministic fallback wording.

        Parameters
        ----------
        directive : DialogueDirective
            Safe dialogue instruction to render.

        Returns
        -------
        str
            User-facing speech text.
        """

        directive_type = directive.directive_type
        if directive_type in {"followup_after_commit", "repair_prompt"}:
            return directive.question_text
        if directive_type == "candidate_question":
            return directive.question_text
        if directive_type == "realtime_converse":
            return directive.question_text or (
                "我听到了，也在顺着你刚才说的内容理解。"
                "如果后面想起需要补充或更正的地方，你随时都可以告诉我。"
            )
        if directive_type == "observer_probe":
            return directive.question_text
        if directive_type == "partial_ack":
            return directive.question_text or "嗯，我在听，你可以继续说。"
        if directive_type in {"meta_answer", "question_clarification"}:
            return directive.question_text
        if directive_type == "free_chat":
            return "嗯，我听到了。你可以接着说。"
        if directive_type == "resume_prompt":
            return f"好，我们继续。{directive.question_text}"
        if directive_type == "crisis":
            return (
                "我听到你可能正处在危险或非常痛苦的状态。这个系统不能处理紧急危机。"
                "请你现在优先保证安全，尽快联系当地紧急服务、身边可信任的人，"
                "或者专业机构。"
            )
        if directive_type == "complete":
            return "本阶段的 SCID 访谈已经完成了。谢谢你的配合，我们先停在这里。"
        if directive_type in {"ask", "clarify"}:
            progress = f"{directive.progress_text}。" if directive.progress_text else ""
            return f"{progress}{directive.question_text}"
        raise ValueError(f"Unsupported dialogue directive type: {directive_type!r}")


class SmallLMDialogueModel(DialogueModel):
    """Use the configured small/frontstage chat model for natural wording."""

    SYSTEM_PROMPT = """你是 SCID 语音模式的前台对话助手，只负责把后台 directive 转成自然、温和、适合语音交流的口语。
你不能评分，不能推断诊断，不能透露或发明表单分数、字段、JSON 或内部流程。为了自然你可以用长一些的句子，但一定要表达出后台directive所需的原意。

不同 directive_type 的规则：
ask/clarify/resume_prompt：只问或重问 directive 指定的问题，可以稍微缓和语气，但不要改变题意。
realtime_converse：严格只输出一个完整短句，直接回应用户刚才的话，并严格基于用户原话做克制承接；目标是一点五到两点五秒朗读长度。可以表达理解或回应流程体验，但不要每轮固定说“之后想起例外可以补充”。不能提出新问题，不能重问当前题，不能补充用户没有说过的经历或感受，也不要宣布题目完成或开始诊断。不要用“好的”“明白”“我想确认一下”等几个字单独成句，也不要用很短的开场语加逗号。
followup_after_commit/repair_prompt：只口语化 question_text；直接从问题正文开始，不要添加“我想再确认一下”“接下来想问”等开场套话。这是后台确认后的下一步或自然返工，不要添加诊断信息。
candidate_question：只自然转述候选问题并直接开问，不要添加短开场套话；不能说上一题已经完成、已经判定或已经记分。
observer_probe：只自然问出 question_text 指定的同一话题追问。不能自行增加第二个问题，不能宣布上一题已经完成，也不能改变时间范围或症状含义。
meta_answer/question_clarification/partial_ack：只把 question_text 变成自然口语，不添加诊断信息。
free_chat：只根据 question_text 里的用户原话做普通、简短回应；不要提 SCID、量表、评估流程、分数或诊断。
crisis/complete：按 directive 给出的安全或结束意图回应。

输出必须适合 TTS 朗读，避免项目符号、表格、JSON、括号和长句。"""

    def __init__(self, model: BaseChatModel) -> None:
        self.model = model
        self.fallback = RuleBasedDialogueModel()

    async def render(self, directive: DialogueDirective) -> str:
        """Render a directive by collecting streamed model output.

        Parameters
        ----------
        directive : DialogueDirective
            Safe dialogue instruction to render.

        Returns
        -------
        str
            Combined model output, or deterministic fallback wording when the
            model produces no text.
        """

        chunks: list[str] = []
        async for chunk in self.stream(directive):
            chunks.append(chunk)
        text = "".join(chunks).strip()
        return text or await self.fallback.render(directive)

    async def stream(
        self,
        directive: DialogueDirective,
        context: Any | None = None,
    ) -> AsyncIterator[str]:
        """Stream natural wording for a safe dialogue directive.

        Parameters
        ----------
        directive : DialogueDirective
            Safe dialogue instruction to render.
        context : Any | None, optional
            Additional context after removal of prohibited clinical fields.

        Yields
        ------
        str
            Model-generated text chunks, or one deterministic fallback chunk
            if streaming fails or produces no text.
        """

        yielded = False
        try:
            async for text in self._stream_model_output(directive, context=context):
                yielded = True
                yield text
            if yielded:
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "SCID frontend model stream failed after_output=%s "
                "directive_type=%s error_type=%s",
                yielded,
                directive.directive_type,
                type(exc).__name__,
            )
            if yielded:
                raise FrontendStreamInterrupted.from_exception(exc) from None

        fallback_text = await self.fallback.render(directive)
        if fallback_text:
            yield fallback_text

    async def render_cache_candidate(
        self, directive: DialogueDirective
    ) -> tuple[str, bool]:
        """Avoid permanently caching fallback text after model failure."""

        try:
            chunks = [
                text
                async for text in self._stream_model_output(directive, context=None)
            ]
            text = "".join(chunks).strip()
            if text:
                return text, True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "SCID candidate frontend rendering failed directive_type=%s "
                "error_type=%s",
                directive.directive_type,
                type(exc).__name__,
            )
        return await self.fallback.render(directive), False

    async def _stream_model_output(
        self,
        directive: DialogueDirective,
        *,
        context: Any | None,
    ) -> AsyncIterator[str]:
        if directive.directive_type == "realtime_converse":
            rendering_request = (
                "请生成一个完整、简短的实时承接句。直接回应用户原话，保持自然、亲切、克制的口语风格，类似知心朋友的回应。"
                "但绝对不要提出任何新问题，也不要输出第二句。"
            )
        else:
            rendering_request = "请把以下 directive 转成一句或两句自然口语。"
        async for chunk in self.model.astream(
            [
                SystemMessage(content=self.SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"{rendering_request}"
                        "严格遵守 directive_type 对应的边界，不要评分，不要诊断。\n"
                        f"directive={directive.snapshot()}\n"
                        f"context={_safe_context_snapshot(context)}"
                    )
                ),
            ]
        ):
            text = str(getattr(chunk, "content", "") or "")
            if text:
                yield text


def _safe_context_snapshot(context: Any | None) -> Any:
    """Return a bounded, allowlisted context payload for the frontend model."""

    if context is None:
        return {}
    if not isinstance(context, dict) and not (
        is_dataclass(context) and not isinstance(context, type)
    ):
        return {}
    budget = _ContextBudget()
    try:
        safe = _sanitize_context(context, depth=0, budget=budget)
    except Exception as exc:
        logger.warning(
            "SCID frontend context serialization failed error_type=%s",
            type(exc).__name__,
        )
        return {}
    return {} if safe is _OMIT else safe


class _ContextBudget:
    """Track aggregate context size without serializing the original object."""

    __slots__ = ("chars", "items")

    def __init__(self) -> None:
        self.chars = _MAX_CONTEXT_TOTAL_CHARS
        self.items = _MAX_CONTEXT_ITEMS


def _sanitize_context(value: Any, *, depth: int, budget: _ContextBudget) -> Any:
    if depth > _MAX_CONTEXT_DEPTH or budget.items <= 0 or budget.chars <= 0:
        return _OMIT

    if value is None or isinstance(value, (bool, int, float)):
        budget.items -= 1
        return value
    if isinstance(value, str):
        budget.items -= 1
        length = min(len(value), _MAX_CONTEXT_STRING_CHARS, budget.chars)
        budget.chars -= length
        return value[:length]

    if is_dataclass(value) and not isinstance(value, type):
        value = {
            item.name: getattr(value, item.name)
            for item in fields(value)
            if item.name in _SAFE_CONTEXT_KEYS
        }

    if isinstance(value, dict):
        budget.items -= 1
        safe_mapping: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_CONTEXT_COLLECTION_ITEMS:
                break
            if budget.items <= 0 or budget.chars <= 0:
                break
            if not isinstance(key, str) or key not in _SAFE_CONTEXT_KEYS:
                continue
            safe_item = _sanitize_context(item, depth=depth + 1, budget=budget)
            if safe_item is not _OMIT:
                safe_mapping[key] = safe_item
        return safe_mapping

    if isinstance(value, (list, tuple)):
        budget.items -= 1
        safe_items: list[Any] = []
        for index, item in enumerate(value):
            if index >= _MAX_CONTEXT_COLLECTION_ITEMS:
                break
            if budget.items <= 0 or budget.chars <= 0:
                break
            safe_item = _sanitize_context(item, depth=depth + 1, budget=budget)
            if safe_item is not _OMIT:
                safe_items.append(safe_item)
        return safe_items

    return _OMIT


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
