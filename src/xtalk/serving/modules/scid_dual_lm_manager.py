"""X-Talk serving bridge for the SCID dual-LM runtime."""

from __future__ import annotations

import asyncio
import math
import unicodedata
from contextlib import suppress
from pathlib import Path
from typing import Any, AsyncIterator

from ...log_utils import logger
from ...pipelines import Pipeline
from ...psych_sop.scid.dialogue.frontend import dialogue_model_from_pipeline_agent
from ...psych_sop.scid.core.product_contract import INPUT_TOO_LONG_ZH
from ...psych_sop.scid.orchestration.runtime import SCIDDualLMRuntime
from ...psych_sop.scid.orchestration.supervisor import SupervisedTask
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

    _ALLOWED_CONFIG_KEYS = frozenset(
        {
            "scid_allow_one_step_speculation",
            "scid_backend_model",
            "scid_deepseek",
            "scid_deepseek_api_key",
            "scid_deepseek_base_url",
            "scid_experiment_id",
            "scid_observer_confidence_threshold",
            "scid_observer_mode",
            "scid_observer_model",
            "scid_partial_observer_debounce_seconds",
            "scid_partial_plan_max_age_seconds",
            "scid_persist_raw_transcript",
            "scid_post_initial_action_wait_seconds",
            "scid_prefer_deepseek",
        }
    )
    _REMOVED_CONFIG_KEYS = frozenset(
        {
            "scid_candidate_pregeneration",
            "scid_enable_wait_text",
            "scid_fast_policy_enabled",
            "scid_frontend_initial_timeout_seconds",
            "scid_frontend_streaming",
            "scid_optimistic_scan",
            "scid_realtime_action_grace_seconds",
            "scid_realtime_action_timeout_seconds",
            "scid_realtime_observer_planning_enabled",
            "scid_runtime_mode",
        }
    )
    _TTS_SOFT_DELIMITERS = frozenset({"，", ",", "：", ":"})
    _TTS_HARD_DELIMITERS = frozenset({"。", "！", "!", "？", "?", "."})
    _MIN_TTS_CLAUSE_CHARS = 12
    _MAX_ASR_FINAL_CHARS = 8192
    _MAX_ASR_PARTIAL_CHARS = 2048
    _PARTIAL_TASK_DRAIN_TIMEOUT_SECONDS = 0.1
    _TASK_DRAIN_TIMEOUT_SECONDS = 1.0
    _INPUT_TOO_LONG_TEXT = INPUT_TOO_LONG_ZH

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
        self._validate_config()
        data_dir = Path(str(self.config.get("data_dir") or "data"))
        get_agent = getattr(pipeline, "get_agent", None)
        agent = get_agent() if callable(get_agent) else None
        dialogue_model = dialogue_model_from_pipeline_agent(agent)
        self.runtime = SCIDDualLMRuntime(
            experiment_id=self._config_string("scid_experiment_id", "scid_voice_demo"),
            backend_model=self._config_string("scid_backend_model", "deepseek-v4-pro"),
            observer_model=self._config_string(
                "scid_observer_model", "deepseek-v4-flash"
            ),
            prefer_deepseek=self._config_bool("scid_prefer_deepseek", True),
            deepseek_api_key=self._deepseek_api_key(),
            deepseek_base_url=self._deepseek_base_url(),
            episode_dir=data_dir / "psych_sop_demo" / "episodes",
            dialogue_model=dialogue_model,
            observer_mode=self._config_choice(
                "scid_observer_mode",
                "shadow",
                choices=frozenset({"off", "shadow", "active"}),
            ),
            allow_one_step_speculation=self._config_bool(
                "scid_allow_one_step_speculation",
                False,
            ),
            observer_confidence_threshold=self._config_float(
                "scid_observer_confidence_threshold",
                0.9,
                minimum=0.0,
                maximum=1.0,
            ),
            partial_plan_max_age_seconds=self._config_float(
                "scid_partial_plan_max_age_seconds",
                3.0,
                minimum=0.0,
                maximum=60.0,
            ),
            post_initial_action_wait_seconds=self._config_float(
                "scid_post_initial_action_wait_seconds",
                0.35,
                minimum=0.0,
                maximum=5.0,
            ),
            persist_raw_transcript=self._config_bool(
                "scid_persist_raw_transcript",
                False,
            ),
        )
        self._started = False
        self._start_lock = asyncio.Lock()
        self._interaction_seq = 0
        self._active_asr_tasks: set[asyncio.Task[None]] = set()
        self._pending_asr_partial_first_at: float | None = None
        self._partial_observer_task: asyncio.Task[None] | None = None
        self._input_rejection_task: asyncio.Task[None] | None = None
        self._partial_observer_debounce_seconds = self._config_float(
            "scid_partial_observer_debounce_seconds",
            0.5,
            minimum=0.0,
            maximum=10.0,
        )

    def _validate_config(self) -> None:
        """Reject unknown, removed, or malformed SCID configuration."""

        removed = sorted(self._REMOVED_CONFIG_KEYS.intersection(self.config))
        if removed:
            keys = ", ".join(removed)
            raise ValueError(
                f"Removed SCID configuration key(s): {keys}. "
                "SCID now always uses the realtime runtime. Replace the former "
                "Fast Policy/optimistic scan switches with "
                "scid_allow_one_step_speculation (default: false); remove all "
                "other listed keys."
            )

        unknown = sorted(
            key
            for key in self.config
            if isinstance(key, str)
            and key.startswith("scid_")
            and key not in self._ALLOWED_CONFIG_KEYS
        )
        if unknown:
            raise ValueError("Unknown SCID configuration key(s): " + ", ".join(unknown))

        # Validate every supported value eagerly, before constructing models or
        # reading credentials. Helpers deliberately never include values in
        # their error messages.
        self._config_string("scid_experiment_id", "scid_voice_demo")
        self._config_string("scid_backend_model", "deepseek-v4-pro")
        self._config_string("scid_observer_model", "deepseek-v4-flash")
        self._config_bool("scid_prefer_deepseek", True)
        self._config_bool("scid_allow_one_step_speculation", False)
        self._config_bool("scid_persist_raw_transcript", False)
        self._config_choice(
            "scid_observer_mode",
            "shadow",
            choices=frozenset({"off", "shadow", "active"}),
        )
        self._config_float(
            "scid_observer_confidence_threshold",
            0.9,
            minimum=0.0,
            maximum=1.0,
        )
        self._config_float(
            "scid_partial_plan_max_age_seconds",
            3.0,
            minimum=0.0,
            maximum=60.0,
        )
        self._config_float(
            "scid_post_initial_action_wait_seconds",
            0.35,
            minimum=0.0,
            maximum=5.0,
        )
        self._config_float(
            "scid_partial_observer_debounce_seconds",
            0.5,
            minimum=0.0,
            maximum=10.0,
        )
        self._validate_deepseek_config()

    def _config_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if type(value) is not bool:
            raise ValueError(f"{key} must be a boolean")
        return value

    def _config_string(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        if len(value) > 2048:
            raise ValueError(f"{key} must be at most 2048 characters")
        return value.strip()

    def _config_choice(
        self,
        key: str,
        default: str,
        *,
        choices: frozenset[str],
    ) -> str:
        value = self._config_string(key, default)
        if value not in choices:
            raise ValueError(f"{key} must be one of: {', '.join(sorted(choices))}")
        return value

    def _config_float(
        self,
        key: str,
        default: float,
        *,
        minimum: float,
        maximum: float,
    ) -> float:
        value = self.config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a finite number")
        try:
            parsed = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"{key} must be a finite number") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"{key} must be a finite number")
        if not minimum <= parsed <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        return parsed

    def _validate_deepseek_config(self) -> None:
        value = self.config.get("scid_deepseek", {})
        if not isinstance(value, dict):
            raise ValueError("scid_deepseek must be an object")
        if any(key not in {"api_key", "base_url"} for key in value):
            raise ValueError("scid_deepseek contains unsupported configuration key(s)")
        for key in ("api_key", "base_url"):
            if key not in value:
                continue
            nested = value[key]
            if not isinstance(nested, str) or not nested.strip():
                raise ValueError(f"scid_deepseek.{key} must be a non-empty string")
            if len(nested) > 2048:
                raise ValueError(f"scid_deepseek.{key} must be at most 2048 characters")

        for key in ("scid_deepseek_api_key", "scid_deepseek_base_url"):
            if key not in self.config:
                continue
            self._config_string(key, "unused")

    def _deepseek_config(self) -> dict[str, Any]:
        value = self.config.get("scid_deepseek")
        return value if isinstance(value, dict) else {}

    def _deepseek_api_key(self) -> str | None:
        key = self.config.get("scid_deepseek_api_key")
        if not key:
            key = self._deepseek_config().get("api_key")
        if not key:
            return None
        return str(key).strip()

    def _deepseek_base_url(self) -> str:
        base_url = self.config.get("scid_deepseek_base_url")
        if not base_url:
            base_url = self._deepseek_config().get("base_url")
        return str(base_url or "https://api.deepseek.com").strip()

    @Manager.event_handler(LLMAgentLoop, priority=30)
    async def _handle_llm_agent_loop(self, event: LLMAgentLoop) -> None:
        """Emit the initial SCID greeting/question when the session starts."""

        del event
        await self._ensure_started()

    async def _ensure_started(self) -> None:
        """Publish the product boundary exactly once before accepting text."""

        async with self._start_lock:
            if self._started:
                return
            logger.info(
                "SCID session start - session: %s, episode: %s",
                self.session_id,
                self.runtime.episode_id,
            )
            opening = await self.runtime.start()
            if not await self._publish_response_text(opening):
                raise RuntimeError("SCID product boundary could not be published")
            self._started = True

    @Manager.event_handler(ASRResultPartial, priority=0)
    async def _handle_asr_result_partial(self, event: ASRResultPartial) -> None:
        """Record partial timing and debounce asynchronous Observer planning."""

        text, too_long = self._normalize_asr_text(
            event.text,
            maximum_chars=self._MAX_ASR_PARTIAL_CHARS,
        )
        if too_long:
            await self._cancel_partial_observer_task()
            self._pending_asr_partial_first_at = None
            self.runtime.reject_asr_partial(reason="input_too_long")
            logger.warning(
                "SCID ASR partial rejected because it is too long - session: %s, chars: %s",
                self.session_id,
                len(text),
            )
            return
        if not text:
            await self._cancel_partial_observer_task()
            self._pending_asr_partial_first_at = None
            self.runtime.reject_asr_partial(reason="empty_after_normalization")
            return
        if self._pending_asr_partial_first_at is None:
            self._pending_asr_partial_first_at = event.timestamp
        if self.runtime.observer_mode == "off":
            return
        await self._cancel_partial_observer_task()
        prospective_seq = self._interaction_seq + 1
        self._partial_observer_task = asyncio.create_task(
            self._observe_partial_after_debounce(
                text=text,
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

        text, too_long = self._normalize_asr_text(
            event.text,
            maximum_chars=self._MAX_ASR_FINAL_CHARS,
        )
        await self._cancel_partial_observer_task()
        if self._input_rejection_task is not None:
            self._input_rejection_task.cancel()
            self._input_rejection_task = None
        if too_long:
            self._interaction_seq += 1
            self._pending_asr_partial_first_at = None
            self.runtime.reject_asr_partial(reason="input_too_long")
            self.runtime.reject_asr_final(
                self._interaction_seq,
                timestamp=event.timestamp,
                reason="input_too_long",
            )
            prior_tasks = list(self._active_asr_tasks)
            for task in prior_tasks:
                task.cancel()
            logger.warning(
                "SCID ASR final rejected because it is too long - session: %s, chars: %s",
                self.session_id,
                len(text),
            )
            task = asyncio.create_task(
                self._publish_input_too_long_prompt(prior_tasks=prior_tasks)
            )
            self._input_rejection_task = task
            self._active_asr_tasks.add(task)
            task.add_done_callback(self._handle_asr_task_done)
            return
        if not text:
            self._interaction_seq += 1
            self._pending_asr_partial_first_at = None
            self.runtime.reject_asr_partial(reason="empty_after_normalization")
            self.runtime.reject_asr_final(
                self._interaction_seq,
                timestamp=event.timestamp,
                reason="empty_after_normalization",
            )
            return
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

    @staticmethod
    def _normalize_asr_text(
        value: Any,
        *,
        maximum_chars: int,
    ) -> tuple[str, bool]:
        """Normalize ASR text without allowing invisible control payloads."""

        if not isinstance(value, str):
            return "", False
        normalized = unicodedata.normalize("NFC", value)
        cleaned = "".join(
            " " if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char
            for char in normalized
        )
        text = " ".join(cleaned.split())
        return text, len(text) > maximum_chars

    async def _cancel_partial_observer_task(self) -> None:
        """Cancel and reap Manager-owned partial Observer debounce work."""

        task = self._partial_observer_task
        self._partial_observer_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        done, pending = await asyncio.wait(
            {task},
            timeout=self._PARTIAL_TASK_DRAIN_TIMEOUT_SECONDS,
        )
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        if pending:
            logger.warning(
                "SCID partial observer did not stop promptly - session: %s",
                self.session_id,
            )

    async def _publish_input_too_long_prompt(
        self,
        *,
        prior_tasks: list[asyncio.Task[None]],
    ) -> None:
        """Stop superseded output and publish a deterministic segmentation prompt."""

        if prior_tasks:
            await asyncio.wait(
                prior_tasks,
                timeout=self._TASK_DRAIN_TIMEOUT_SECONDS,
            )
        await self.event_bus.publish(
            TurnLLMAgentStopRequested(
                session_id=self.session_id,
                reason="scid_asr_input_too_long",
            ),
            wait_for_completion=True,
        )
        await self._ensure_started()
        await self._publish_response_text(self._INPUT_TOO_LONG_TEXT)

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
        if task is self._input_rejection_task:
            self._input_rejection_task = None
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
        await self._ensure_started()
        try:
            response = await self.runtime.accept_text(
                text,
                interaction_seq=interaction_seq,
            )
        except Exception:
            logger.exception(
                "SCID runtime failed unexpectedly - session: %s, episode: %s",
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
                "SCID response skipped because it is stale - session: %s, episode: %s, seq: %s",
                self.session_id,
                self.runtime.episode_id,
                interaction_seq,
            )
            return
        if response.initial_stream is None and response.action_stream_task is None:
            return
        delivered = await self._publish_response_stream(
            self._runtime_turn_stream(
                interaction_seq=interaction_seq,
                initial_stream=response.initial_stream,
                action_stream_task=response.action_stream_task,
            ),
            action_stream_task=response.action_stream_task,
        )
        if delivered and response.action_stream_task is not None:
            # EventBus success only proves that the combined iterator ended
            # normally.  A scored turn with an action stream is deliverable
            # after either a non-terminal timeout bridge or a real action
            # segment was actually emitted.
            delivered = self.runtime.action_followup_was_published(interaction_seq)
        # Closing a never-started async generator does not enter its ``finally``
        # block.  Record the terminal delivery outcome explicitly so a publish
        # failure cannot leave a completed/crisis episode stuck as partial.
        await self.runtime.acomplete_response_delivery(
            interaction_seq,
            success=delivered,
        )

    async def _runtime_turn_stream(
        self,
        *,
        interaction_seq: int,
        initial_stream: AsyncIterator[str] | None,
        action_stream_task: (
            asyncio.Task[AsyncIterator[str] | None]
            | SupervisedTask[AsyncIterator[str] | None]
            | None
        ),
    ) -> AsyncIterator[str]:
        action_stream: AsyncIterator[str] | None = None
        try:
            initial_published = False
            if initial_stream is not None:
                logger.info(
                    "SCID runtime turn stream started - session: %s, episode: %s, seq: %s",
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
                action_stream = await action_stream_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "SCID runtime action stream task failed - session: %s, episode: %s, seq: %s",
                    self.session_id,
                    self.runtime.episode_id,
                    interaction_seq,
                )
                try:
                    action_stream = await self.runtime.recover_action_stream_failure(
                        interaction_seq,
                        exc,
                    )
                except Exception:
                    logger.exception(
                        "SCID action stream recovery failed - session: %s, episode: %s, seq: %s",
                        self.session_id,
                        self.runtime.episode_id,
                        interaction_seq,
                    )
                    return
            if not self.runtime.is_latest_interaction(interaction_seq):
                self.runtime.mark_interaction_stale(interaction_seq)
                logger.info(
                    "SCID runtime action stream stale - session: %s, episode: %s, seq: %s",
                    self.session_id,
                    self.runtime.episode_id,
                    interaction_seq,
                )
                return
            if action_stream is None:
                return
            logger.info(
                "SCID runtime action appended to turn stream - session: %s, episode: %s, seq: %s",
                self.session_id,
                self.runtime.episode_id,
                interaction_seq,
            )
            action_published = False
            async for chunk in action_stream:
                if not self.runtime.is_latest_interaction(interaction_seq):
                    self.runtime.mark_interaction_stale(interaction_seq)
                    return
                if chunk and not action_published:
                    action_published = True
                    self.runtime.mark_followup_segment_published(interaction_seq)
                if chunk:
                    yield chunk
        finally:
            await self._close_stream(initial_stream)
            await self._close_stream(action_stream)
            if action_stream_task is not None and not action_stream_task.done():
                action_stream_task.cancel()
                with suppress(asyncio.CancelledError):
                    await action_stream_task
            await self.runtime.release_turn(interaction_seq)

    async def _publish_response_text(self, text: str) -> bool:
        """Publish a text-only agent stream for existing LLM/TTS consumers."""

        if not text:
            return True
        return await self._publish_response_stream(self._single_text_stream(text))

    async def _publish_response_stream(
        self,
        stream: AsyncIterator[str],
        *,
        action_stream_task: (
            asyncio.Task[AsyncIterator[str] | None]
            | SupervisedTask[AsyncIterator[str] | None]
            | None
        ) = None,
    ) -> bool:
        """Publish an agent stream for existing LLM/TTS consumers."""

        published_stream = self._coalesce_short_tts_clauses(stream)
        error: Exception | None = None
        try:
            published = await self.event_bus.publish(
                ConsumeLLMAgentGenerationRequested(
                    session_id=self.session_id,
                    stream=published_stream,
                ),
                wait_for_completion=True,
            )
        except Exception as exc:
            error = exc
            published = False
        if published:
            return True

        await self._close_stream(published_stream)
        await self._close_stream(stream)
        if action_stream_task is not None and not action_stream_task.done():
            action_stream_task.cancel()
            await asyncio.gather(action_stream_task, return_exceptions=True)
        logger.error(
            "Failed to publish SCID response - session: %s, error: %s",
            self.session_id,
            error or "event bus returned false",
        )
        return False

    @staticmethod
    async def _close_stream(stream: AsyncIterator[str] | None) -> None:
        """Close an owned async iterator when delivery stops early."""

        close = getattr(stream, "aclose", None)
        if callable(close):
            with suppress(RuntimeError):
                await close()

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
        if partial_task is not None:
            partial_task.cancel()
            self._partial_observer_task = None
        tasks = list(self._active_asr_tasks)
        if partial_task is not None:
            tasks.append(partial_task)
        for task in tasks:
            task.cancel()
        await self.runtime.aclose(status="aborted")
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=self._TASK_DRAIN_TIMEOUT_SECONDS,
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            if pending:
                logger.warning(
                    "SCID manager shutdown detached %s cancellation-resistant task(s) - session: %s",
                    len(pending),
                    self.session_id,
                )
