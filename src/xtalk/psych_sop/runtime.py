"""Reusable runtime for the Psychology SOP demo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .counseling_agent import CounselingAgent
from .episode_logger import DEFAULT_EPISODE_DIR, EpisodeLogger
from .evolution_summarizer import EvolutionSummarizer
from .memory_backend import (
    DEBUG_USER_ID,
    DEFAULT_MEMORY_PATH,
    PsychMemoryBackend,
    create_memory_backend,
)
from .safety_guard import SafetyGuard
from .scale_engine import ScaleEngine
from .sop_navigator import SOPNavigator


SUPPORTED_RUNNABLE_SCALES = {"GAD-7", "PHQ-9"}
TERMINAL_NODES = {"SUPPORTIVE_CLOSE", "CRISIS_RESPONSE", "ABORTED"}


def normalize_scale_id(value: str) -> str:
    """Normalize user-facing scale ids."""

    text = value.strip().upper().replace("_", "-")
    aliases = {"GAD7": "GAD-7", "PHQ9": "PHQ-9", "SCL90": "SCL-90"}
    return aliases.get(text, text)


def select_scale(user_text: str, default_scale: str) -> str | None:
    """Select a runnable scale from free-form user text."""

    text = user_text.strip()
    if not text:
        return default_scale
    normalized = normalize_scale_id(text)
    if normalized in SUPPORTED_RUNNABLE_SCALES:
        return normalized
    if "焦虑" in text:
        return "GAD-7"
    if "抑郁" in text or "情绪" in text or "低落" in text:
        return "PHQ-9"
    return None


def is_skip(text: str) -> bool:
    """Return whether the user asked to skip the current item."""

    return text.strip().lower() in {"跳过", "略过", "skip"}


def is_explain(text: str) -> bool:
    """Return whether the user asked for a neutral item explanation."""

    lowered = text.strip().lower()
    return lowered in {"解释", "说明", "什么意思", "help"} or "解释" in lowered


def build_metadata(
    *,
    scope: str,
    scale_id: str,
    experiment_id: str,
    sop_version: str,
    prompt_version: str,
    result: str | None = None,
    failure_type: str | None = None,
) -> dict[str, Any]:
    """Build standard PsychSOP memory metadata."""

    return {
        "scope": scope,
        "task": "psych_sop_scale_demo",
        "scale_id": scale_id,
        "experiment_id": experiment_id,
        "sop_version": sop_version,
        "prompt_version": prompt_version,
        "result": result,
        "failure_type": failure_type,
    }


class PsychSOPRuntime:
    """Session runtime shared by CLI and X-Talk serving integration."""

    def __init__(
        self,
        *,
        scale_id: str = "GAD-7",
        experiment_id: str = "psych_sop_demo",
        user_id: str = DEBUG_USER_ID,
        prefer_mem0: bool = True,
        reset_memory: bool = False,
        memory_backend: PsychMemoryBackend | None = None,
        memory_path: str | Path = DEFAULT_MEMORY_PATH,
        episode_dir: str | Path = DEFAULT_EPISODE_DIR,
        sop_path: str | Path | None = None,
    ) -> None:
        self.default_scale = normalize_scale_id(scale_id)
        if self.default_scale not in SUPPORTED_RUNNABLE_SCALES:
            raise ValueError("PsychSOPRuntime only supports GAD-7 and PHQ-9 for now.")

        self.experiment_id = experiment_id
        self.user_id = user_id
        self.navigator = SOPNavigator.from_yaml(sop_path)
        self.agent = CounselingAgent()
        self.safety_guard = SafetyGuard()
        self.memory = memory_backend or create_memory_backend(
            prefer_mem0=prefer_mem0,
            path=memory_path,
            user_id=user_id,
        )
        if reset_memory:
            self.memory.reset()

        self.selected_scale = self.default_scale
        self.engine = ScaleEngine()
        self.scale = self.engine.load_scale(self.selected_scale)
        self.logger = EpisodeLogger(
            user_id=user_id,
            task="psych_sop_scale_demo",
            scale_id=self.selected_scale,
            sop_version=self.navigator.sop_spec.sop_id,
            prompt_version=self.agent.prompt_version,
            experiment_id=experiment_id,
            episode_dir=episode_dir,
        )

        self.score: int | None = None
        self.interpretation: dict[str, Any] | None = None
        self.status = "failed"
        self.failure_type: str | None = None
        self.evolution_summary: str | None = None
        self._started = False
        self._finished = False
        self._episode_path: Path | None = None
        self._last_assistant = ""

    @property
    def is_finished(self) -> bool:
        """Return whether the runtime reached and saved a terminal episode."""

        return self._finished

    @property
    def episode_path(self) -> Path | None:
        """Return the saved episode path, when available."""

        return self._episode_path

    def start(self) -> str:
        """Render the first assistant turn without consuming user input."""

        self._started = True
        return self._set_last_assistant(self._render_current())

    def accept_text(self, user_text: str) -> str:
        """Accept one user text turn and return the next assistant text."""

        if self._finished:
            return self._last_assistant
        if not self._started:
            self.start()

        node = self.navigator.current_node()
        log_node_id = node.id
        previous_assistant = self._last_assistant

        safety = self.agent_safety_classify(user_text)
        if safety["should_interrupt_sop"]:
            self._record_safety_event(safety)
            self._log_and_remember(
                node_id=log_node_id,
                action="safety_interrupt",
                assistant_text=previous_assistant,
                user_text=user_text,
            )
            self.navigator.step(user_text, {"safety_interrupt": True})
            self.status = "crisis"
            self.failure_type = "safety_interrupt"
            return self._terminal_response()

        if safety["risk_level"] != "none":
            self._record_safety_event(safety)

        context: dict[str, Any] = {}
        retry_text = self._prepare_node_context(node.id, user_text, context)
        if retry_text is not None:
            self._log_and_remember(
                node_id=log_node_id,
                action="retry",
                assistant_text=previous_assistant,
                user_text=user_text,
            )
            return self._set_last_assistant(retry_text)

        next_action = self.navigator.step(user_text, context)
        self._log_and_remember(
            node_id=log_node_id,
            action=next_action.action,
            assistant_text=previous_assistant,
            user_text=user_text,
        )
        self._apply_post_transition(user_text)

        if self.navigator.current_node().id in TERMINAL_NODES:
            return self._terminal_response()
        return self._set_last_assistant(self._render_current())

    def finish(
        self,
        status: str | None = None,
        failure_type: str | None = None,
    ) -> Path:
        """Save episode log and evolution memory. Idempotent."""

        if self._episode_path is not None:
            self._finished = True
            return self._episode_path

        final_status = status or self.status
        final_failure_type = failure_type if failure_type is not None else self.failure_type
        if final_status == "failed" and final_failure_type is None:
            final_failure_type = "unexpected_exit"
        if final_status == "aborted" and self.engine.state:
            if self.engine.state.status == "in_progress":
                self.engine.abort_scale(final_failure_type or "aborted")

        answers = self.engine.state.answers if self.engine.state else {}
        skipped = self.engine.state.skipped if self.engine.state else []
        self.logger.set_result(
            status=final_status,
            answers=answers,
            skipped_questions=skipped,
            score=self.score,
            interpretation=self.interpretation,
            failure_type=final_failure_type,
            dropout=final_status in {"aborted", "failed"},
        )
        if self._last_assistant:
            self.logger.add_note(f"last_assistant={self._last_assistant[:200]}")
        self._episode_path = self.logger.save()
        self.evolution_summary = EvolutionSummarizer().summarize(self.logger.episode)
        self.memory.add_note(
            self.evolution_summary,
            scope="evolution_memory",
            metadata=self._metadata(
                scope="evolution_memory",
                result=final_status,
                failure_type=final_failure_type,
            ),
        )
        self.status = final_status
        self.failure_type = final_failure_type
        self._finished = True
        return self._episode_path

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable runtime snapshot."""

        return {
            "selected_scale": self.selected_scale,
            "current_node": self.navigator.current_node().id,
            "score": self.score,
            "interpretation": self.interpretation,
            "status": self.status,
            "failure_type": self.failure_type,
            "is_finished": self.is_finished,
            "episode_path": str(self.episode_path) if self.episode_path else None,
            "scale_state": self.engine.snapshot(),
        }

    def agent_safety_classify(self, user_text: str) -> dict[str, Any]:
        """Classify safety risk and return a JSON-friendly dict."""

        result = self.safety_guard.classify(user_text)
        return {
            "risk_level": result.risk_level,
            "risk_type": result.risk_type,
            "matched_signals": result.matched_signals,
            "should_interrupt_sop": result.should_interrupt_sop,
        }

    def _prepare_node_context(
        self,
        node_id: str,
        user_text: str,
        context: dict[str, Any],
    ) -> str | None:
        if node_id == "SCALE_SELECTION":
            maybe_scale = select_scale(user_text, self.default_scale)
            if maybe_scale is None:
                return "目前请在 GAD-7 和 PHQ-9 中选择一个。"
            self.selected_scale = maybe_scale
            self.scale = self.engine.load_scale(self.selected_scale)
            self.logger.episode["scale_id"] = self.selected_scale
            context["selected_scale"] = self.selected_scale

        if node_id == "RISK_CHECK":
            context["high_risk"] = False

        if node_id in {"SCALE_LOOP", "CLARIFY_ITEM"}:
            if node_id == "CLARIFY_ITEM":
                self.navigator.set_node("SCALE_LOOP")
            retry_text = self._prepare_scale_answer_context(user_text, context)
            if retry_text is not None:
                return retry_text
        return None

    def _prepare_scale_answer_context(
        self,
        user_text: str,
        context: dict[str, Any],
    ) -> str | None:
        if is_explain(user_text):
            context["needs_clarification"] = True
            self.logger.increment_clarification()
            return None
        if is_skip(user_text):
            if self.engine.state is None:
                return "当前量表还没有开始，请先继续流程。"
            self.engine.skip_question(self.engine.state.current_index)
            context["user_skips"] = True
            return None

        option_id, confidence, reason = self.engine.parse_answer(user_text)
        if option_id is None or confidence < 0.6:
            self.logger.increment_clarification()
            return (
                "我还不能确定你的选择。请回复 0、1、2、3，"
                "或输入“解释”“跳过”“退出”。"
            )
        if self.engine.state is None:
            return "当前量表还没有开始，请先继续流程。"
        self.engine.record_answer(
            self.engine.state.current_index,
            option_id,
            user_text,
            confidence=confidence,
        )
        context["answer_recorded"] = True
        context["answer_parse_reason"] = reason
        return None

    def _apply_post_transition(self, user_text: str) -> None:
        current_node_id = self.navigator.current_node().id
        if current_node_id == "SCALE_LOOP" and self.engine.state is None:
            self.scale = self.engine.load_scale(self.selected_scale)
            self.engine.start_scale(self.selected_scale)
            return

        if current_node_id == "RECORD_ANSWER":
            has_next = self.engine.has_next_question()
            if has_next:
                self.engine.next_question()
            self.navigator.step(user_text, {"has_next_question": has_next})
            current_node_id = self.navigator.current_node().id

        if current_node_id == "COMPUTE_SCORE":
            self.score = self.engine.compute_score()
            self.interpretation = self.engine.get_score_interpretation(self.score)
            self.navigator.step(user_text, {"score_computed": True})
            self.memory.add_note(
                (
                    f"{self.selected_scale} 最近一次量表结果：总分 {self.score}，"
                    f"解释：{self.interpretation.get('label')}。"
                ),
                scope="scale_state_summary",
                metadata=self._metadata(
                    scope="scale_state_summary",
                    result="completed",
                ),
            )

    def _terminal_response(self) -> str:
        node_id = self.navigator.current_node().id
        text = self._set_last_assistant(self._render_current())
        self.status = {
            "SUPPORTIVE_CLOSE": "completed",
            "CRISIS_RESPONSE": "crisis",
            "ABORTED": "aborted",
        }[node_id]
        if node_id == "ABORTED" and self.failure_type is None:
            self.failure_type = "user_abort"
        self.finish(status=self.status, failure_type=self.failure_type)
        return text

    def _render_current(self) -> str:
        node = self.navigator.current_node()
        memory_context = self.memory.search(
            query=f"{self.selected_scale} {node.id}",
            scope="dialogue_memory",
            top_k=3,
        )
        return self.agent.render(
            node_id=node.id,
            action=(node.allowed_actions[0] if node.allowed_actions else "noop"),
            scale_title=self.scale.title,
            current_question=(
                self.engine.get_current_question()
                if self.engine.state
                and self.engine.state.status == "in_progress"
                and node.id in {"SCALE_LOOP", "CLARIFY_ITEM"}
                else None
            ),
            options=self.engine.get_options() if self.engine.scale else [],
            progress=(
                self.engine.get_progress()
                if self.engine.state and self.engine.state.status == "in_progress"
                else None
            ),
            score=self.score,
            interpretation=self.interpretation,
            crisis_response=self.navigator.sop_spec.crisis_response,
            memory_context=memory_context,
            selected_scale=self.selected_scale,
        )

    def _record_safety_event(self, safety: dict[str, Any]) -> None:
        self.logger.add_safety_event(
            {
                "risk_level": safety["risk_level"],
                "risk_type": safety["risk_type"],
                "matched_signals": safety["matched_signals"],
            }
        )

    def _log_and_remember(
        self,
        *,
        node_id: str,
        action: str,
        assistant_text: str,
        user_text: str,
    ) -> None:
        self.logger.add_turn(
            node_id=node_id,
            action=action,
            assistant_text=assistant_text,
            user_text=user_text,
        )
        self.memory.add_dialogue_turn(
            user_text,
            assistant_text,
            metadata=self._metadata(scope="dialogue_memory"),
        )

    def _metadata(
        self,
        *,
        scope: str,
        result: str | None = None,
        failure_type: str | None = None,
    ) -> dict[str, Any]:
        return build_metadata(
            scope=scope,
            scale_id=self.selected_scale,
            experiment_id=self.experiment_id,
            sop_version=self.navigator.sop_spec.sop_id,
            prompt_version=self.agent.prompt_version,
            result=result,
            failure_type=failure_type,
        )

    def _set_last_assistant(self, text: str) -> str:
        self._last_assistant = text
        return text
