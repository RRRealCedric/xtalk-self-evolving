"""Rule-based episode-level evolution summary."""

from __future__ import annotations

from typing import Any


class EvolutionSummarizer:
    """Generate a compact self-evolution note.

    TODO(evolution): replace this with an evaluator that proposes SOP/prompt
    patches for human review.
    """

    def summarize(self, episode: dict[str, Any]) -> str:
        scale_id = episode.get("scale_id", "量表")
        status = episode.get("status", "unknown")
        clarification_count = int(episode.get("clarification_count") or 0)
        skipped = episode.get("skipped_questions") or []
        failure_type = episode.get("failure_type")

        if status == "completed":
            return (
                f"本次 {scale_id} 引导已完成。用户完成了量表，"
                f"跳过 {len(skipped)} 题，澄清 {clarification_count} 次。"
                "建议后续继续观察用户在哪些题目需要解释，并优化题目解释话术。"
            )
        if status == "crisis":
            return (
                f"本次 {scale_id} 引导因安全风险中断。Agent 已停止量表并进入危机回应。"
                "建议后续细化危机识别规则和本地化危机资源文案。"
            )
        if status == "aborted":
            return (
                f"本次 {scale_id} 引导未完成。用户中途退出。"
                f"失败类型为 {failure_type or 'user_abort'}。"
                "建议下次退出前提供更短的继续选项，并允许用户保存当前进度。"
            )
        return (
            f"本次 {scale_id} 引导状态为 {status}。"
            "建议人工检查 episode log，补充更精确的失败原因。"
        )
