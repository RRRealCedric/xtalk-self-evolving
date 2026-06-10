"""X-Talk serving bridge for the Psychology SOP runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

from ...log_utils import logger
from ...pipelines import Pipeline
from ...psych_sop.runtime import PsychSOPRuntime
from ..event_bus import EventBus
from ..events import (
    ASRResultFinal,
    ConsumeLLMAgentGenerationRequested,
    LLMAgentLoop,
)
from ..interfaces import Manager


class PsychSOPManager(Manager):
    """Route ASR text into ``PsychSOPRuntime`` and reuse TTS downstream."""

    def __init__(
        self,
        event_bus: EventBus,
        session_id: str,
        pipeline: Pipeline,
        config: dict[str, Any] | None = None,
    ) -> None:
        del pipeline
        self.event_bus = event_bus
        self.session_id = session_id
        self.config = config or {}
        data_dir = Path(str(self.config.get("data_dir") or "data"))
        self.runtime = PsychSOPRuntime(
            scale_id=str(self.config.get("psych_sop_scale") or "GAD-7"),
            experiment_id=str(
                self.config.get("psych_sop_experiment_id") or "psych_sop_voice_demo"
            ),
            prefer_mem0=bool(self.config.get("psych_sop_prefer_mem0", True)),
            reset_memory=bool(self.config.get("psych_sop_reset_memory", False)),
            memory_path=data_dir / "psych_sop_demo" / "memory.json",
            episode_dir=data_dir / "psych_sop_demo" / "episodes",
        )
        self._started = False

    @Manager.event_handler(LLMAgentLoop, priority=30)
    async def _handle_llm_agent_loop(self, event: LLMAgentLoop) -> None:
        """Emit the initial SOP greeting when the session starts."""

        del event
        if self._started:
            return
        self._started = True
        await self._publish_response_text(self.runtime.start())

    @Manager.event_handler(ASRResultFinal, priority=30)
    async def _handle_asr_result_final(self, event: ASRResultFinal) -> None:
        """Accept final ASR text and publish the next SOP response."""

        text = (event.text or "").strip()
        if not text:
            return
        if not self._started:
            self.runtime.start()
            self._started = True
        await self._publish_response_text(self.runtime.accept_text(text))

    async def _publish_response_text(self, text: str) -> None:
        """Publish a text-only agent stream for existing LLM/TTS consumers."""

        if not text:
            return
        try:
            await self.event_bus.publish(
                ConsumeLLMAgentGenerationRequested(
                    session_id=self.session_id,
                    stream=self._single_text_stream(text),
                ),
                wait_for_completion=True,
            )
        except Exception as exc:
            logger.error(
                "Failed to publish PsychSOP response - session: %s, error: %s",
                self.session_id,
                exc,
            )

    @staticmethod
    async def _single_text_stream(text: str) -> AsyncIterator[str]:
        yield text

    async def shutdown(self) -> None:
        if self._started and not self.runtime.is_finished:
            self.runtime.finish(status="aborted", failure_type="service_shutdown")
