"""Background diagnostic-LM adapters for SCID decisions."""

from __future__ import annotations

import os
import json
from abc import ABC, abstractmethod

from langchain.chat_models.base import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ...log_utils import logger
from .decision import (
    DecisionParseError,
    fallback_reask_decision,
    parse_assessment_decision,
)
from .ledger import AssessmentLedger
from .schema import AssessmentDecision, normalize_score


class BackgroundAssessor(ABC):
    """Interface for the background diagnostic decision maker."""

    @abstractmethod
    async def assess(
        self,
        *,
        ledger: AssessmentLedger,
        user_text: str,
        turn_id: int,
    ) -> AssessmentDecision:
        """Return one SCID decision for the current turn."""


class RuleBasedAssessor(BackgroundAssessor):
    """Small offline assessor used for tests and no-key development."""

    async def assess(
        self,
        *,
        ledger: AssessmentLedger,
        user_text: str,
        turn_id: int,
    ) -> AssessmentDecision:
        del turn_id
        field_id = ledger.current_field_id or ""
        text = user_text.strip().lower()
        if any(word in text for word in ("自杀", "不想活", "伤害自己", "杀了别人")):
            return AssessmentDecision(
                field_id=field_id,
                score=None,
                confidence=0.9,
                evidence=[user_text],
                next_action="crisis",
                clarification_question="",
                reasoning_summary="用户表达了高风险安全信号。",
            )
        if not text or any(word in text for word in ("不知道", "不确定", "说不清")):
            return AssessmentDecision(
                field_id=field_id,
                score=None,
                confidence=0.3,
                evidence=[],
                next_action="clarify",
                clarification_question="你能再具体说说有没有发生过、频率大概怎样吗？",
                reasoning_summary="当前回答不足以判定。",
            )

        negative = ("没有", "没", "从未", "不是", "不算", "否")
        threshold = ("有", "经常", "总是", "严重", "明显", "影响")
        subthreshold = ("一点", "偶尔", "有时", "轻微")
        if any(word in text for word in negative):
            score = "1"
            confidence = 0.72
        elif any(word in text for word in threshold):
            score = "3"
            confidence = 0.72
        elif any(word in text for word in subthreshold):
            score = "2"
            confidence = 0.65
        else:
            score = "?"
            confidence = 0.45

        return AssessmentDecision(
            field_id=field_id,
            score=normalize_score(score),
            confidence=confidence,
            evidence=[user_text],
            next_action="advance",
            clarification_question="",
            reasoning_summary="根据用户当前回答做出的低复杂度离线判断。",
        )


class DeepSeekAssessor(BackgroundAssessor):
    """DeepSeek V4 Pro assessor using OpenAI-compatible chat completions."""

    SYSTEM_PROMPT = """你是后台心理评估流程执行器，不是前台聊天助手。
你的任务是严格根据 SCID 当前节点、字段、用户原话、历史证据和已填状态，
输出一个 JSON 对象，供程序校验后写入表单。你不能输出自然语言段落。
你可以在内部充分推理，但 reasoning_summary 只能给出非诊断性简短依据。

评分规则：
? = 资料不足；1 = 无或否；2 = 阈下；3 = 阈上或是。
如果信息不足，使用 next_action=clarify 或 reask，不要猜测。
如果用户表达自伤、自杀、伤害他人或立即危险，使用 next_action=crisis。
如果足以判定当前字段，使用 advance。若流程需要进入分支，可用 branch。

必须只输出 JSON，且字段完全为：
{
  "field_id": "S1-F3",
  "score": "3",
  "confidence": 0.82,
  "evidence": ["用户说..."],
  "next_action": "advance | clarify | reask | branch | crisis",
  "clarification_question": "",
  "reasoning_summary": "非诊断性简短依据"
}
"""

    def __init__(
        self,
        *,
        model: str = "deepseek-v4-pro",
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        temperature: float = 0.1,
        max_tokens: int = 1200,
        chat_model: BaseChatModel | None = None,
    ) -> None:
        self.model_name = model
        self.chat_model = chat_model or ChatOpenAI(
            model=model,
            api_key=api_key or os.getenv("DEEPSEEK_API_KEY"),
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            model_kwargs={
                "response_format": {"type": "json_object"},
            },
        )

    async def assess(
        self,
        *,
        ledger: AssessmentLedger,
        user_text: str,
        turn_id: int,
    ) -> AssessmentDecision:
        field_id = ledger.current_field_id or ""
        logger.info(
            "SCID backend assess start - model: %s, field: %s, turn: %s",
            self.model_name,
            field_id,
            turn_id,
        )
        payload = ledger.build_assessor_context(
            ledger._turn_by_id(turn_id)  # noqa: SLF001 - shared runtime object.
        )
        prompt = (
            "请根据以下 JSON 上下文输出本轮 SCID decision。"
            "注意：只输出 JSON 对象。\n\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )
        raw = await self._invoke_text(prompt)
        logger.info(
            "SCID backend raw response received - field: %s, turn: %s, chars: %s",
            field_id,
            turn_id,
            len(raw),
        )
        try:
            decision = parse_assessment_decision(raw)
            logger.info(
                "SCID backend decision parsed - field: %s, turn: %s, action: %s, score: %s",
                decision.field_id,
                turn_id,
                decision.next_action,
                decision.score,
            )
            return decision
        except DecisionParseError as exc:
            logger.warning(
                "SCID backend decision parse failed; attempting repair - field: %s, turn: %s, error: %s",
                field_id,
                turn_id,
                exc,
            )
            repaired = await self._repair_json(raw)
            try:
                decision = parse_assessment_decision(repaired)
                logger.info(
                    "SCID backend repaired decision parsed - field: %s, turn: %s, action: %s, score: %s",
                    decision.field_id,
                    turn_id,
                    decision.next_action,
                    decision.score,
                )
                return decision
            except DecisionParseError as exc:
                logger.warning(
                    "SCID backend JSON repair failed - field: %s, turn: %s, error: %s",
                    field_id,
                    turn_id,
                    exc,
                )
                return fallback_reask_decision(
                    field_id=field_id,
                    reason=f"后台模型 JSON 解析失败：{exc}",
                )

    async def _invoke_text(self, prompt: str) -> str:
        response = await self.chat_model.ainvoke(
            [
                SystemMessage(content=self.SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ]
        )
        return str(response.content or "")

    async def _repair_json(self, raw_text: str) -> str:
        response = await self.chat_model.ainvoke(
            [
                SystemMessage(
                    content=(
                        "你是 JSON 修复器。把用户提供的文本修复为一个合法 JSON 对象。"
                        "只输出 JSON，不要解释。"
                    )
                ),
                HumanMessage(content=raw_text),
            ]
        )
        return str(response.content or "")


def create_background_assessor(
    *,
    model: str = "deepseek-v4-pro",
    prefer_deepseek: bool = True,
) -> BackgroundAssessor:
    """Create the configured assessor.

    Without ``DEEPSEEK_API_KEY`` we use a deterministic fallback so local tests
    and demos still run without accidentally requiring a secret.
    """

    if prefer_deepseek and os.getenv("DEEPSEEK_API_KEY"):
        return DeepSeekAssessor(model=model)
    return RuleBasedAssessor()
