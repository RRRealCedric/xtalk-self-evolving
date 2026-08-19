"""Versioned product-boundary contract for the SCID research runtime."""

from __future__ import annotations


PRODUCT_CONTRACT_VERSION = "0.1.0"
DEPLOYMENT_SCOPE = "research-only"

USER_DISCLOSURE_ZH = (
    "开始前说明：这是一次用结构化问题帮助你梳理感受和生活影响的非诊断性对话。"
    "它不会因为普通的烦躁或压力就给你任何诊断，也不能替代医生或心理治疗师。"
    "你可以随时暂停或结束。"
    "如果你正处于危险状况，请优先联系当地紧急服务或身边可信任的人。"
)

SESSION_STOPPED_ZH = "可以，我们先结束这次评估。谢谢你的配合。"

SESSION_COMPLETED_ZH = "本阶段的结构化访谈已经完成了。谢谢你的配合，我们先停在这里。"

CRISIS_RESPONSE_ZH = (
    "我听到你可能正处在危险或非常痛苦的状态。这个系统不能处理紧急危机。"
    "请你现在优先保证安全，尽快联系当地紧急服务、身边可信任的人，"
    "或者具备危机处置能力的专业机构。"
)

INPUT_TOO_LONG_ZH = "这段内容比较长，我没法可靠地一次处理。请把它分成几段再告诉我。"


def render_session_opening(question_text: str) -> str:
    """Prepend the versioned non-diagnostic disclosure to the first question.

    Parameters
    ----------
    question_text : str
        Rendered first structured interview question.

    Returns
    -------
    str
        User-facing disclosure followed by the first question, when present.
    """

    question = question_text.strip()
    if not question:
        return USER_DISCLOSURE_ZH
    return f"{USER_DISCLOSURE_ZH}\n\n{question}"
