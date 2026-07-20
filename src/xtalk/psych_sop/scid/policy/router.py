"""Fast control-plane routing before SCID scoring."""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from typing import Any

from langchain.chat_models.base import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ....log_utils import logger
from ..assessment.decision import DecisionParseError, extract_json_object_text
from ..core.schema import (
    SCIDRouteDecision,
    VALID_INTERACTION_ROUTES,
)


class RouteParseError(ValueError):
    """Raised when a backend router response cannot be parsed."""


class SCIDInteractionRouter(ABC):
    """Detect control intents before background SCID understanding happens.

    This is intentionally no longer a clinical-evidence gate. Non-control
    utterances are allowed through to the background assessor/observer path so
    that natural conversation is not discarded before clinical understanding.
    """

    @abstractmethod
    async def route(self, *, context: dict[str, Any]) -> SCIDRouteDecision:
        """Return a route decision for one user-facing interaction."""


def parse_route_decision(text: str) -> SCIDRouteDecision:
    """Parse model text into a ``SCIDRouteDecision``."""

    try:
        payload = json.loads(extract_json_object_text(text))
    except (json.JSONDecodeError, DecisionParseError) as exc:
        raise RouteParseError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise RouteParseError("Route payload must be a JSON object")
    return route_decision_from_payload(payload)


def route_decision_from_payload(payload: dict[str, Any]) -> SCIDRouteDecision:
    """Build a typed route decision from a JSON-like dict."""

    missing = [
        key
        for key in (
            "route",
            "confidence",
            "should_score",
            "normalized_user_text",
            "safe_frontend_content",
            "reasoning_summary",
        )
        if key not in payload
    ]
    if missing:
        raise RouteParseError(f"Route payload missing keys: {missing}")

    route = str(payload.get("route") or "").strip()
    if route not in VALID_INTERACTION_ROUTES:
        raise RouteParseError(f"Invalid route: {route!r}")

    confidence = float(payload.get("confidence") or 0.0)
    confidence = max(0.0, min(1.0, confidence))
    should_score = route == "scid_answer"

    return SCIDRouteDecision(
        route=route,  # type: ignore[arg-type]
        confidence=confidence,
        should_score=should_score,
        normalized_user_text=str(payload.get("normalized_user_text") or "").strip(),
        safe_frontend_content=str(payload.get("safe_frontend_content") or "").strip(),
        reasoning_summary=str(payload.get("reasoning_summary") or "").strip(),
        raw_payload=dict(payload),
    )


class RuleBasedSCIDInteractionRouter(SCIDInteractionRouter):
    """Small deterministic control detector for tests and local development."""

    async def route(self, *, context: dict[str, Any]) -> SCIDRouteDecision:
        """Route one interaction with deterministic control-intent rules.

        Parameters
        ----------
        context : dict[str, Any]
            Interaction mode, current field, pending buffer, and user text.

        Returns
        -------
        SCIDRouteDecision
            Control-plane route and safe foreground response metadata.
        """

        text = str(context.get("user_text") or "").strip()
        mode = str(context.get("interaction_mode") or "scid")
        pending = str(context.get("pending_user_buffer") or "").strip()
        normalized = _combine_pending(pending, text)

        if _contains_any(text, ("自杀", "不想活", "伤害自己", "杀了别人")):
            return _route(
                "crisis",
                text,
                "用户表达了可能的安全风险。",
                safe="我听到你可能正处在危险或非常痛苦的状态。我们先把安全放在第一位。",
                confidence=0.95,
            )

        if _contains_any(
            text, ("退出", "结束评估", "不做了", "停止评估", "stop", "quit")
        ):
            return _route(
                "stop_scid",
                text,
                "用户要求结束评估。",
                safe="可以，我们先结束这次评估。谢谢你刚才的配合。",
                confidence=0.9,
            )

        if _is_meta_question(text):
            return _route(
                "meta_question",
                text,
                "用户询问评估流程而非回答当前字段。",
                safe=_meta_answer(context),
                confidence=0.88,
            )

        if _requests_pause(text):
            return _route(
                "pause_scid",
                text,
                "用户要求暂时打断或暂停。",
                safe="可以，我们先暂停一下。你想继续时直接说继续就好。",
                confidence=0.82,
            )

        if _requests_resume(text):
            return _route(
                "resume_scid",
                text,
                "用户表示可以回到评估流程。",
                safe="好的，我们回到刚才的问题。",
                confidence=0.86,
            )

        if _contains_any(
            text, ("什么意思", "什么叫", "能解释", "怎么理解", "这个问题")
        ):
            return _route(
                "question_clarification",
                text,
                "用户要求解释当前问题。",
                safe=_clarification_answer(context),
                confidence=0.82,
            )

        if _looks_incomplete(text):
            return _route(
                "scid_partial",
                normalized,
                "用户话语未完成，暂不进入判分。",
                safe="嗯，我在听，你可以继续说。",
                confidence=0.78,
            )

        if mode in {"paused"} and not _looks_like_scid_answer(text):
            return _route(
                "off_sop_chat",
                text,
                "用户在暂停状态下继续普通对话，暂不恢复评估。",
                safe="",
                confidence=0.72,
            )

        return _route(
            "scid_answer",
            normalized,
            "非控制输入进入后台观察与保守判分路径。",
            should_score=True,
            confidence=0.74,
        )


class DeepSeekSCIDInteractionRouter(SCIDInteractionRouter):
    """Experimental DeepSeek-backed control detector."""

    SYSTEM_PROMPT = """你是 SCID 语音评估系统的控制平面检测器，不是判分器，也不是前台聊天助手。
你只识别会改变系统行为的控制意图，不负责判断一句话是否有临床价值。

你会看到当前 SCID 节点、当前对话模式、用户原话、未完成回答缓存和最近交互。
如果用户没有明确暂停、继续、停止、危机、流程提问、题意澄清或未完成表达，
一律输出 route=scid_answer 且 should_score=true，让后台 assessor/observer 继续理解。
不要因为内容看似闲聊或背景叙述就输出 off_sop_chat。
如果用户问流程问题，例如还要问多少题，输出 route=meta_question，并在 safe_frontend_content 给出安全、简短、不含分数和诊断的回答。
如果用户要求解释当前题，输出 route=question_clarification。
如果用户话没说完，输出 route=scid_partial。
如果用户要求暂停、继续、停止，分别输出 pause_scid、resume_scid、stop_scid。
“为什么不继续问”是在询问系统行为，应输出 meta_question；“先不继续”“不要继续问”是暂停，不能因为包含“继续”而输出 resume_scid。
如果用户表达自伤、自杀、伤害他人或立即危险，输出 crisis。
只有在 interaction_mode=paused 且用户没有表示继续时，才可输出 off_sop_chat。

必须只输出 JSON，且字段完全为：
{
  "route": "scid_answer | scid_partial | question_clarification | meta_question | off_sop_chat | resume_scid | pause_scid | stop_scid | crisis",
  "confidence": 0.86,
  "should_score": true,
  "normalized_user_text": "用户可用于判分的合并文本",
  "safe_frontend_content": "给前台复述的安全内容，不能含诊断或分数",
  "reasoning_summary": "非诊断性简短依据"
}
"""

    def __init__(
        self,
        *,
        model: str = "deepseek-v4-pro",
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        temperature: float = 0.0,
        max_tokens: int = 900,
        chat_model: BaseChatModel | None = None,
    ) -> None:
        self.model_name = model
        self.fallback = RuleBasedSCIDInteractionRouter()
        self.chat_model = chat_model or ChatOpenAI(
            model=model,
            api_key=api_key or os.getenv("DEEPSEEK_API_KEY"),
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            model_kwargs={"response_format": {"type": "json_object"}},
        )

    async def route(self, *, context: dict[str, Any]) -> SCIDRouteDecision:
        """Route one interaction with DeepSeek and a rule-based fallback.

        Parameters
        ----------
        context : dict[str, Any]
            Interaction mode, current field, pending buffer, and user text.

        Returns
        -------
        SCIDRouteDecision
            Parsed model route, or a deterministic route if model processing
            fails.
        """

        logger.info(
            "SCID router start - model: %s, seq: %s, mode: %s",
            self.model_name,
            context.get("interaction_seq"),
            context.get("interaction_mode"),
        )
        prompt = (
            "请根据以下 JSON 上下文输出本轮 SCID route decision。"
            "注意：只输出 JSON 对象。\n\n"
            f"{json.dumps(context, ensure_ascii=False, indent=2)}"
        )
        try:
            raw = await self._invoke_text(prompt)
            decision = parse_route_decision(raw)
            logger.info(
                "SCID router parsed - seq: %s, route: %s, should_score: %s",
                context.get("interaction_seq"),
                decision.route,
                decision.should_score,
            )
            return decision
        except Exception as exc:
            logger.warning(
                "SCID router failed; using rule fallback - seq: %s, error: %s",
                context.get("interaction_seq"),
                exc,
            )
            return await self.fallback.route(context=context)

    async def _invoke_text(self, prompt: str) -> str:
        response = await self.chat_model.ainvoke(
            [
                SystemMessage(content=self.SYSTEM_PROMPT),
                HumanMessage(content=prompt),
            ]
        )
        return str(response.content or "")


def create_scid_interaction_router(
    *,
    model: str = "deepseek-v4-pro",
    prefer_deepseek: bool = True,
) -> SCIDInteractionRouter:
    """Create the configured control detector.

    Phase 0-2 plus the router first step keep control routing local and fast.
    DeepSeek-backed routing remains available as an experimental class, but the
    default path should not put a remote router before frontend responsiveness or
    background clinical understanding.
    """

    del model, prefer_deepseek
    return RuleBasedSCIDInteractionRouter()


def _route(
    route: str,
    normalized_text: str,
    reason: str,
    *,
    safe: str = "",
    should_score: bool = False,
    confidence: float = 0.7,
) -> SCIDRouteDecision:
    return SCIDRouteDecision(
        route=route,  # type: ignore[arg-type]
        confidence=confidence,
        should_score=should_score,
        normalized_user_text=normalized_text,
        safe_frontend_content=safe,
        reasoning_summary=reason,
        raw_payload={"fallback_rule": True, "route": route},
    )


def _combine_pending(pending: str, text: str) -> str:
    if not pending:
        return text
    if text.startswith(pending) or pending in text:
        return text
    return f"{pending}{text}"


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


def _requests_pause(text: str) -> bool:
    return _contains_any(
        text,
        (
            "暂停",
            "等一下",
            "先停",
            "休息一下",
            "打断一下",
            "先不继续",
            "暂时不继续",
            "不继续这个",
            "先别继续",
            "别继续",
            "先别问",
            "不要继续问",
        ),
    )


def _requests_resume(text: str) -> bool:
    if _is_meta_question(text) or _contains_any(
        text,
        (
            "不继续",
            "别继续",
            "不要继续",
            "不能继续",
            "先不",
            "暂时不",
        ),
    ):
        return False
    return _contains_any(
        text,
        (
            "继续吧",
            "继续问",
            "可以继续",
            "我们继续",
            "接着问",
            "往下问",
            "回到刚才",
        ),
    ) or text.strip() in {"继续", "接着", "可以"}


def _is_meta_question(text: str) -> bool:
    return _asks_why_interview_did_not_continue(text) or _contains_any(
        text,
        (
            "多少个问题",
            "多少题",
            "还要问多久",
            "问多久",
            "为什么问",
            "为什么每次",
            "为什么总是",
            "为什么不继续",
            "为什么没继续",
            "为什么没有继续",
            "怎么不继续",
            "为什么不问",
            "为什么没问",
            "为什么没有问",
            "为什么继续问",
            "每次都",
            "总是说",
            "老是说",
            "好的我记下了",
            "好的，我记下了",
            "你要问什么",
            "流程",
            "量表",
            "问卷",
            "可以不回答",
        ),
    )


def _asks_why_interview_did_not_continue(text: str) -> bool:
    """Match natural variants such as '为什么你刚才不继续问' safely."""

    compact = re.sub(r"[\s，,。！？!?、]", "", text)
    return bool(
        re.search(
            r"(?:为什么|怎么).{0,12}(?:不|没|没有).{0,8}"
            r"(?:继续|接着|往下|问|提问|说话)",
            compact,
        )
    )


def _looks_incomplete(text: str) -> bool:
    stripped = text.strip()
    if stripped in {"有", "没", "否", "对", "是"}:
        return False
    if len(stripped) <= 1:
        return True
    if stripped.endswith(
        ("比如说", "就是", "我就会", "然后", "因为", "呃", "嗯", "就")
    ):
        return True
    return bool(re.search(r"(比如说|就是|因为|然后)[，, ]*$", stripped))


def _looks_like_scid_answer(text: str) -> bool:
    return _contains_any(
        text,
        (
            "有",
            "没有",
            "没",
            "对",
            "不是",
            "一点",
            "经常",
            "偶尔",
            "担心",
            "害怕",
            "焦虑",
            "紧张",
            "不确定",
            "不知道",
        ),
    )


def _meta_answer(context: dict[str, Any]) -> str:
    text = str(context.get("user_text") or "")
    if _asks_why_interview_did_not_continue(text) or _contains_any(
        text,
        (
            "为什么不继续",
            "为什么没继续",
            "为什么没有继续",
            "怎么不继续",
            "为什么不问",
            "为什么没问",
            "为什么没有问",
        ),
    ):
        return (
            "刚才系统在等待后台核对你上一段回答，所以承接之后没有及时接上问题。"
            "这是处理延迟，不是你没有回答。你希望现在继续，还是先停一下？"
        )
    if _contains_any(
        text,
        (
            "为什么每次",
            "为什么总是",
            "每次都",
            "总是说",
            "老是说",
            "好的我记下了",
            "好的，我记下了",
        ),
    ):
        return (
            "你说得对，刚才回应有点重复。我会尽量更自然一点；"
            "同时后台还会继续核对你刚才的回答。"
        )
    progress = context.get("progress") or {}
    current = progress.get("current")
    total = progress.get("total")
    if isinstance(current, int) and isinstance(total, int) and total > 0:
        return (
            f"这个扫描阶段最多会问 {total} 个主题性问题，"
            f"现在大约在第 {current} 个。后面只会对需要进一步确认的部分追问，"
            "你也可以随时暂停。"
        )
    return "这个阶段会按主题逐步确认，你可以随时暂停；准备好后我们再继续。"


def _clarification_answer(context: dict[str, Any]) -> str:
    current = context.get("current_node") or {}
    question = str(current.get("question_text") or "").strip()
    if question:
        return f"我是在确认这个情况是否发生过。你可以按自己的理解说有、没有，或者举一个例子。刚才的问题是：{question}"
    return (
        "我是在确认这个情况是否发生过。你可以按自己的理解说有、没有，或者举一个例子。"
    )
