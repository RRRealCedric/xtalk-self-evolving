"""X-Talk serving bridge for the SCID dual-LM runtime."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any, AsyncIterator

from ...log_utils import logger
from ...pipelines import Pipeline
from ...psych_sop.scid.dialogue.frontend import dialogue_model_from_pipeline_agent
from ...psych_sop.scid.orchestration.runtime import SCIDDualLMRuntime
from ..event_bus import EventBus
from ..events import (
    ASRResultFinal,
    ASRResultPartial,
    ConsumeLLMAgentGenerationRequested,
    LLMAgentLoop,
    TurnLLMAgentStopRequested,
)
from ..interfaces import Manager


class SCIDDualLMManager(Manager):
    """Route ASR text into the SCID runtime and reuse downstream TTS."""

    _TTS_SOFT_DELIMITERS = frozenset({"，", ",", "：", ":"})
    _TTS_HARD_DELIMITERS = frozenset({"。", "！", "!", "？", "?", "."})
    _MIN_TTS_CLAUSE_CHARS = 12

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
        self.runtime_mode = str(self.config.get("scid_runtime_mode") or "sequential")
        if self.runtime_mode not in {"sequential", "parallel", "realtime"}:
            self.runtime_mode = "sequential"
        self.runtime = SCIDDualLMRuntime(
            experiment_id=str(
                self.config.get("scid_experiment_id") or "scid_voice_demo"
            ),
            backend_model=str(
                self.config.get("scid_backend_model") or "deepseek-v4-pro"
            ),
            observer_model=str(
                self.config.get("scid_observer_model") or "deepseek-v4-flash"
            ),
            prefer_deepseek=bool(self.config.get("scid_prefer_deepseek", True)),
            deepseek_api_key=self._deepseek_api_key(),
            deepseek_base_url=self._deepseek_base_url(),
            episode_dir=data_dir / "psych_sop_demo" / "episodes",
            dialogue_model=dialogue_model,
            enable_wait_text=bool(self.config.get("scid_enable_wait_text", True)),
            runtime_mode=self.runtime_mode,
            observer_mode=str(self.config.get("scid_observer_mode") or "shadow"),
            enable_candidate_pregeneration=bool(
                self.config.get("scid_candidate_pregeneration", True)
            ),
            enable_optimistic_scan=bool(self.config.get("scid_optimistic_scan", False)),
            observer_confidence_threshold=float(
                self.config.get("scid_observer_confidence_threshold", 0.9)
            ),
            frontend_initial_timeout_seconds=float(
                self.config.get("scid_frontend_initial_timeout_seconds", 1.2)
            ),
            frontend_streaming=bool(self.config.get("scid_frontend_streaming", True)),
            fast_policy_enabled=bool(self.config.get("scid_fast_policy_enabled", True)),
            realtime_action_timeout_seconds=float(
                self.config.get("scid_realtime_action_timeout_seconds", 8.0)
            ),
            realtime_action_grace_seconds=float(
                self.config.get("scid_realtime_action_grace_seconds", 1.0)
            ),
            realtime_observer_planning_enabled=bool(
                self.config.get(
                    "scid_realtime_observer_planning_enabled",
                    True,
                )
            ),
            partial_plan_max_age_seconds=float(
                self.config.get("scid_partial_plan_max_age_seconds", 3.0)
            ),
            post_initial_action_wait_seconds=float(
                self.config.get(
                    "scid_post_initial_action_wait_seconds",
                    0.35,
                )
            ),
        )
        self._started = False
        self._interaction_seq = 0
        self._active_asr_tasks: set[asyncio.Task[None]] = set()
        self._pending_asr_partial_first_at: float | None = None
        self._partial_observer_task: asyncio.Task[None] | None = None
        self._partial_observer_debounce_seconds = float(
            self.config.get("scid_partial_observer_debounce_seconds", 0.5)
        )

    def _deepseek_config(self) -> dict[str, Any]:
        value = self.config.get("scid_deepseek")
        return value if isinstance(value, dict) else {}

    def _deepseek_api_key(self) -> str | None:
        key = self.config.get("scid_deepseek_api_key")
        if not key:
            key = self._deepseek_config().get("api_key")
        if not key:
            return None
        return str(key)

    def _deepseek_base_url(self) -> str:
        base_url = self.config.get("scid_deepseek_base_url")
        if not base_url:
            base_url = self._deepseek_config().get("base_url")
        return str(base_url or "https://api.deepseek.com")

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

    @Manager.event_handler(ASRResultPartial, priority=0)
    async def _handle_asr_result_partial(self, event: ASRResultPartial) -> None:
        """Record partial timing and debounce asynchronous Observer planning."""

        if not (event.text or "").strip():
            return
        if self._pending_asr_partial_first_at is None:
            self._pending_asr_partial_first_at = event.timestamp
        if self.runtime.observer_mode == "off":
            return
        if self._partial_observer_task is not None:
            self._partial_observer_task.cancel()
        prospective_seq = self._interaction_seq + 1
        self._partial_observer_task = asyncio.create_task(
            self._observe_partial_after_debounce(
                text=(event.text or "").strip(),
                interaction_seq=prospective_seq,
                delay_seconds=(
                    min(0.05, self._partial_observer_debounce_seconds)
                    if event.speech_pause
                    else self._partial_observer_debounce_seconds
                ),
            )
        )

    @Manager.event_handler(ASRResultFinal, priority=0)
    async def _handle_asr_result_final(self, event: ASRResultFinal) -> None:
        """Schedule final ASR text processing after frontend display handlers."""

        text = (event.text or "").strip()
        if not text:
            return
        if self._partial_observer_task is not None:
            self._partial_observer_task.cancel()
            self._partial_observer_task = None
        self._interaction_seq += 1
        interaction_seq = self._interaction_seq
        if self._pending_asr_partial_first_at is not None:
            self.runtime.record_asr_partial(
                interaction_seq,
                timestamp=self._pending_asr_partial_first_at,
            )
            self._pending_asr_partial_first_at = None
        self.runtime.record_asr_final(interaction_seq, timestamp=event.timestamp)
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

    async def _observe_partial_after_debounce(
        self,
        *,
        text: str,
        interaction_seq: int,
        delay_seconds: float,
    ) -> None:
        try:
            await asyncio.sleep(max(0.0, delay_seconds))
            await self.runtime.observe_asr_partial(
                text,
                interaction_seq=interaction_seq,
            )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "SCID partial observer failed - session: %s, seq: %s",
                self.session_id,
                interaction_seq,
            )

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
        if self.runtime_mode == "parallel":
            await self._process_asr_result_final_parallel(
                text=text,
                interaction_seq=interaction_seq,
            )
            return
        if self.runtime_mode == "realtime":
            await self._process_asr_result_final_realtime(
                text=text,
                interaction_seq=interaction_seq,
            )
            return
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
        except asyncio.CancelledError:
            wait_task.cancel()
            with suppress(asyncio.CancelledError):
                await wait_task
            raise
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

    async def _process_asr_result_final_parallel(
        self,
        *,
        text: str,
        interaction_seq: int,
    ) -> None:
        try:
            response = await self.runtime.accept_text_progressive(
                text,
                interaction_seq=interaction_seq,
            )
        except Exception:
            logger.exception(
                "SCID progressive runtime failed unexpectedly - session: %s, episode: %s",
                self.session_id,
                self.runtime.episode_id,
            )
            await self._publish_response_text(
                "刚才后台流程出现了技术问题，我们先暂停一下，我会重新核对状态。"
            )
            return
        if response.stale:
            self.runtime.mark_interaction_stale(interaction_seq)
            logger.info(
                "SCID progressive response skipped because it is stale - session: %s, episode: %s, seq: %s",
                self.session_id,
                self.runtime.episode_id,
                interaction_seq,
            )
            return
        if not response.initial_text and response.followup_task is None:
            return
        await self._publish_response_stream(
            self._progressive_turn_stream(
                interaction_seq=interaction_seq,
                initial_text=response.initial_text,
                followup_task=response.followup_task,
            )
        )

    async def _process_asr_result_final_realtime(
        self,
        *,
        text: str,
        interaction_seq: int,
    ) -> None:
        try:
            response = await self.runtime.accept_text_realtime(
                text,
                interaction_seq=interaction_seq,
            )
        except Exception:
            logger.exception(
                "SCID realtime runtime failed unexpectedly - session: %s, episode: %s",
                self.session_id,
                self.runtime.episode_id,
            )
            await self._publish_response_text(
                "刚才后台流程出现了技术问题，我们先暂停一下，我会重新核对状态。"
            )
            return
        if response.stale:
            self.runtime.mark_interaction_stale(interaction_seq)
            logger.info(
                "SCID realtime response skipped because it is stale - session: %s, episode: %s, seq: %s",
                self.session_id,
                self.runtime.episode_id,
                interaction_seq,
            )
            return
        if response.initial_stream is None and response.action_stream_task is None:
            return
        await self._publish_response_stream(
            self._realtime_turn_stream(
                interaction_seq=interaction_seq,
                initial_stream=response.initial_stream,
                action_stream_task=response.action_stream_task,
            )
        )

    async def _realtime_turn_stream(
        self,
        *,
        interaction_seq: int,
        initial_stream: AsyncIterator[str] | None,
        action_stream_task: asyncio.Task[AsyncIterator[str] | None] | None,
    ) -> AsyncIterator[str]:
        initial_published = False
        if initial_stream is not None:
            logger.info(
                "SCID realtime turn stream started - session: %s, episode: %s, seq: %s",
                self.session_id,
                self.runtime.episode_id,
                interaction_seq,
            )
            async for chunk in initial_stream:
                if not self.runtime.is_latest_interaction(interaction_seq):
                    self.runtime.mark_interaction_stale(interaction_seq)
                    return
                if chunk and not initial_published:
                    initial_published = True
                    self.runtime.mark_first_segment_published(interaction_seq)
                if chunk:
                    yield chunk

        if action_stream_task is None:
            return
        try:
            stream = await action_stream_task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "SCID realtime action stream task failed - session: %s, episode: %s, seq: %s",
                self.session_id,
                self.runtime.episode_id,
                interaction_seq,
            )
            return
        if not self.runtime.is_latest_interaction(interaction_seq):
            self.runtime.mark_interaction_stale(interaction_seq)
            logger.info(
                "SCID realtime action stream stale - session: %s, episode: %s, seq: %s",
                self.session_id,
                self.runtime.episode_id,
                interaction_seq,
            )
            return
        if stream is None:
            return
        logger.info(
            "SCID realtime action appended to turn stream - session: %s, episode: %s, seq: %s",
            self.session_id,
            self.runtime.episode_id,
            interaction_seq,
        )
        followup_published = False
        async for chunk in stream:
            if not self.runtime.is_latest_interaction(interaction_seq):
                self.runtime.mark_interaction_stale(interaction_seq)
                return
            if chunk and not followup_published:
                followup_published = True
                self.runtime.mark_followup_segment_published(interaction_seq)
            if chunk:
                yield chunk

    async def _progressive_turn_stream(
        self,
        *,
        interaction_seq: int,
        initial_text: str,
        followup_task: asyncio.Task[str] | None,
    ) -> AsyncIterator[str]:
        if initial_text:
            logger.info(
                "SCID progressive initial appended to turn stream - session: %s, episode: %s, seq: %s, chars: %s",
                self.session_id,
                self.runtime.episode_id,
                interaction_seq,
                len(initial_text),
            )
            self.runtime.mark_first_segment_published(interaction_seq)
            yield initial_text

        if followup_task is None:
            return
        try:
            text = await asyncio.shield(followup_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "SCID progressive followup task failed - session: %s, episode: %s, seq: %s",
                self.session_id,
                self.runtime.episode_id,
                interaction_seq,
            )
            return
        if not self.runtime.is_latest_interaction(interaction_seq):
            self.runtime.mark_interaction_stale(interaction_seq)
            logger.info(
                "SCID progressive followup stale - session: %s, episode: %s, seq: %s",
                self.session_id,
                self.runtime.episode_id,
                interaction_seq,
            )
            return
        if not text:
            return
        logger.info(
            "SCID progressive followup appended to turn stream - session: %s, episode: %s, seq: %s, chars: %s",
            self.session_id,
            self.runtime.episode_id,
            interaction_seq,
            len(text),
        )
        self.runtime.mark_followup_segment_published(interaction_seq)
        yield text

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
        await self._publish_response_stream(self._single_text_stream(text))

    async def _publish_response_stream(self, stream: AsyncIterator[str]) -> None:
        """Publish an agent stream for existing LLM/TTS consumers."""

        try:
            await self.event_bus.publish(
                ConsumeLLMAgentGenerationRequested(
                    session_id=self.session_id,
                    stream=self._coalesce_short_tts_clauses(stream),
                ),
                wait_for_completion=True,
            )
        except Exception as exc:
            logger.error(
                "Failed to publish SCID response - session: %s, error: %s",
                self.session_id,
                exc,
            )

    @classmethod
    async def _coalesce_short_tts_clauses(
        cls,
        stream: AsyncIterator[str],
    ) -> AsyncIterator[str]:
        """Avoid tiny comma-delimited TTS jobs that can finish playback early."""

        clause_chars = 0
        async for chunk in stream:
            transformed: list[str] = []
            for char in str(chunk):
                if char in cls._TTS_SOFT_DELIMITERS:
                    if clause_chars < cls._MIN_TTS_CLAUSE_CHARS:
                        transformed.append(" ")
                        continue
                    transformed.append(char)
                    clause_chars = 0
                    continue
                transformed.append(char)
                if char in cls._TTS_HARD_DELIMITERS:
                    clause_chars = 0
                elif not char.isspace():
                    clause_chars += 1
            text = "".join(transformed)
            if text:
                yield text

    @staticmethod
    async def _single_text_stream(text: str) -> AsyncIterator[str]:
        yield text

    async def shutdown(self) -> None:
        """Shut down the manager and cancel outstanding background work.

        Any unfinished SCID runtime is finalized with an ``aborted`` status.
        """

        partial_task = self._partial_observer_task
        if self._partial_observer_task is not None:
            self._partial_observer_task.cancel()
            self._partial_observer_task = None
        self.runtime.cancel_background_tasks()
        tasks = list(self._active_asr_tasks)
        if partial_task is not None:
            tasks.append(partial_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._started and not self.runtime.is_finished:
            self.runtime.finish(status="aborted")
