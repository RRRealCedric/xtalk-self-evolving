"""X-Talk serving bridge for the SCID dual-LM runtime."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any, AsyncIterator

from ...log_utils import logger
from ...pipelines import Pipeline
from ...psych_sop.scid.frontend import dialogue_model_from_pipeline_agent
from ...psych_sop.scid.runtime import SCIDDualLMRuntime
from ..event_bus import EventBus
from ..events import (
    ASRResultFinal,
    ConsumeLLMAgentGenerationRequested,
    LLMAgentLoop,
    TurnLLMAgentStopRequested,
)
from ..interfaces import Manager


class SCIDDualLMManager(Manager):
    """Route ASR text into the SCID runtime and reuse downstream TTS."""

    def __init__(
        self,
        event_bus: EventBus,
        session_id: str,
        pipeline: Pipeline,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.session_id = session_id
        self.pipeline = pipeline
        self.config = config or {}
        data_dir = Path(str(self.config.get("data_dir") or "data"))
        get_agent = getattr(pipeline, "get_agent", None)
        agent = get_agent() if callable(get_agent) else None
        dialogue_model = dialogue_model_from_pipeline_agent(agent)
        self.runtime = SCIDDualLMRuntime(
            experiment_id=str(
                self.config.get("scid_experiment_id") or "scid_voice_demo"
            ),
            backend_model=str(
                self.config.get("scid_backend_model") or "deepseek-v4-pro"
            ),
            prefer_deepseek=bool(self.config.get("scid_prefer_deepseek", True)),
            episode_dir=data_dir / "psych_sop_demo" / "episodes",
            dialogue_model=dialogue_model,
            enable_wait_text=bool(self.config.get("scid_enable_wait_text", True)),
        )
        self._started = False
        self._interaction_seq = 0
        self._active_asr_tasks: set[asyncio.Task[None]] = set()

    @Manager.event_handler(LLMAgentLoop, priority=30)
    async def _handle_llm_agent_loop(self, event: LLMAgentLoop) -> None:
        """Emit the initial SCID greeting/question when the session starts."""

        del event
        if self._started:
            return
        self._started = True
        logger.info(
            "SCID session start - session: %s, episode: %s",
            self.session_id,
            self.runtime.episode_id,
        )
        await self._publish_response_text(await self.runtime.start())

    @Manager.event_handler(ASRResultFinal, priority=0)
    async def _handle_asr_result_final(self, event: ASRResultFinal) -> None:
        """Schedule final ASR text processing after frontend display handlers."""

        text = (event.text or "").strip()
        if not text:
            return
        self._interaction_seq += 1
        interaction_seq = self._interaction_seq
        logger.info(
            "SCID ASR final received - session: %s, episode: %s, seq: %s, chars: %s",
            self.session_id,
            self.runtime.episode_id,
            interaction_seq,
            len(text),
        )
        task = asyncio.create_task(
            self._process_asr_result_final(
                text=text,
                interaction_seq=interaction_seq,
            )
        )
        self._active_asr_tasks.add(task)
        task.add_done_callback(self._handle_asr_task_done)

    def _handle_asr_task_done(self, task: asyncio.Task[None]) -> None:
        self._active_asr_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error(
            "SCID ASR processing task failed - session: %s, episode: %s, error: %s",
            self.session_id,
            self.runtime.episode_id,
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    async def _process_asr_result_final(
        self,
        *,
        text: str,
        interaction_seq: int,
    ) -> None:
        """Run SCID backend work without blocking ASR final frontend delivery."""

        await self.event_bus.publish(
            TurnLLMAgentStopRequested(
                session_id=self.session_id,
                reason="scid_new_asr_final",
            ),
            wait_for_completion=True,
        )
        if not self._started:
            await self.runtime.start()
            self._started = True
        wait_task = asyncio.create_task(
            self._publish_wait_text_after_delay(
                text=text,
                interaction_seq=interaction_seq,
            )
        )
        try:
            response = await self.runtime.accept_text(
                text,
                interaction_seq=interaction_seq,
            )
        except Exception:
            wait_task.cancel()
            with suppress(asyncio.CancelledError):
                await wait_task
            logger.exception(
                "SCID runtime failed unexpectedly - session: %s, episode: %s",
                self.session_id,
                self.runtime.episode_id,
            )
            await self._publish_response_text(
                "刚才后台流程出现了技术问题，我们先暂停一下，我会重新核对状态。"
            )
            return
        wait_task.cancel()
        with suppress(asyncio.CancelledError):
            await wait_task
        if response.stale:
            logger.info(
                "SCID response skipped because it is stale - session: %s, episode: %s, seq: %s",
                self.session_id,
                self.runtime.episode_id,
                interaction_seq,
            )
            return
        if response.final_text:
            logger.info(
                "SCID final text published - session: %s, episode: %s, seq: %s, chars: %s",
                self.session_id,
                self.runtime.episode_id,
                interaction_seq,
                len(response.final_text),
            )
            await self._publish_response_text(response.final_text)

    async def _publish_wait_text_after_delay(
        self,
        *,
        text: str,
        interaction_seq: int,
    ) -> None:
        await asyncio.sleep(0.7)
        if not self.runtime.is_latest_interaction(interaction_seq):
            return
        wait_text = self.runtime.wait_text_for(text)
        if not wait_text:
            return
        logger.info(
            "SCID delayed wait text published - session: %s, episode: %s, seq: %s",
            self.session_id,
            self.runtime.episode_id,
            interaction_seq,
        )
        await self._publish_response_text(wait_text)

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
                "Failed to publish SCID response - session: %s, error: %s",
                self.session_id,
                exc,
            )

    @staticmethod
    async def _single_text_stream(text: str) -> AsyncIterator[str]:
        yield text

    async def shutdown(self) -> None:
        tasks = list(self._active_asr_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._started and not self.runtime.is_finished:
            self.runtime.finish(status="aborted")
