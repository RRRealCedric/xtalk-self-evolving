# -*- coding: utf-8 -*-
from __future__ import annotations

# Playback tracking plan:
# 1. Extend TTSPlaybackManager state with a segment ledger, a FIFO queue of
#    generated chunk durations, the latest reported text prefix, and cached
#    final-response metadata such as the active response text.
# 2. Subscribe to TTSChunkGenerated and TTSTextSynthesized. TTSChunkGenerated
#    is only used to build the FIFO chunk-duration queue for playback acks,
#    while TTSTextSynthesized carries the sentence-level audio duration needed
#    to close each synthesized text segment.
# 3. Consume TTSChunkPlayed to advance playback through the segment ledger.
#    For each confirmed played chunk, reduce the FIFO duration queue, update the
#    active segment's played-audio counter, map the played-audio ratio back to a
#    text prefix, and publish ResponseUpdate whenever that played prefix grows.
# 4. Use a FIFO duration queue rather than a fixed chunk-size assumption. Each
#    TTSChunkGenerated contributes its exact duration, and each TTSChunkPlayed
#    consumes the next duration in order, so playback tracking matches the real
#    emitted audio timing, including short final chunks.
# 5. Tighten ResponseFinish to come from the same playback state. When playback
#    completes, flush any remaining played text via ResponseUpdate if needed,
#    publish ResponseFinish with the cached final text, and clear the playback
#    tracker state for the next turn.

from collections import deque
from dataclasses import dataclass
from typing import Any

from ...log_utils import logger
from ..event_bus import EventBus
from ..events import (
    LLMAgentResponseFinish,
    LLMAgentResponseUpdate,
    ResponseFinish,
    ResponseUpdate,
    TTSChunkReady,
    TTSChunkPlayed,
    TTSPlaybackFinished,
    TTSTextSynthesized,
    TTSStopped,
)
from ..interfaces import Manager


@dataclass
class _PlaybackSegment:
    """One synthesized text segment and its generated/played audio accounting."""

    text: str
    total_audio_ms: float
    played_audio_ms: float = 0.0


class TTSPlaybackManager(Manager):
    """Project confirmed TTS playback progress back onto response text."""

    def __init__(
        self,
        event_bus: EventBus,
        session_id: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.session_id = session_id
        self.config: dict[str, Any] = config or {}
        self._current_response_text = ""
        self._pending_text = ""
        self._segments: deque[_PlaybackSegment] = deque()
        self._generated_chunk_ms: deque[float] = deque()
        self._played_without_segment_ms = 0.0
        self._completed_text = ""
        self._reported_text = ""

    def _reset_playback_tracking(self) -> None:
        """Reset playback-progress state while preserving response metadata."""

        self._segments.clear()
        self._generated_chunk_ms.clear()
        self._played_without_segment_ms = 0.0
        self._completed_text = ""
        self._reported_text = ""

    def _reset_all_state(self) -> None:
        """Reset all cached response and playback-tracking state."""

        self._reset_playback_tracking()
        self._current_response_text = ""
        self._pending_text = ""

    @Manager.event_handler(LLMAgentResponseUpdate, priority=20)
    async def _cache_response_update(self, event: LLMAgentResponseUpdate) -> None:
        """Track the active response text without emitting frontend-facing updates."""

        next_text = event.text or ""
        if self._current_response_text and not next_text.startswith(
            self._current_response_text
        ):
            self._reset_all_state()
        self._current_response_text = next_text

    @Manager.event_handler(TTSChunkReady, priority=20)
    async def _track_generated_chunk(self, event: TTSChunkReady) -> None:
        """Track generated chunk durations in FIFO order for playback acks."""
        chunk_ms = self._chunk_duration_ms(
            audio_chunk=event.audio_chunk,
            sample_rate=event.sample_rate,
        )
        if chunk_ms <= 0.0:
            return
        self._generated_chunk_ms.append(chunk_ms)

    @Manager.event_handler(TTSTextSynthesized, priority=20)
    async def _close_text_segment(self, event: TTSTextSynthesized) -> None:
        """Bind one synthesized text segment to its reported synthesized duration."""
        text = event.text or ""
        if not text:
            return
        self._segments.append(
            _PlaybackSegment(
                text=text,
                total_audio_ms=max(0.0, float(event.audio_duration or 0.0)),
            )
        )
        await self._apply_pending_playback_time()

    @Manager.event_handler(TTSChunkPlayed, priority=20)
    async def _publish_response_update(self, event: TTSChunkPlayed) -> None:
        """Advance played text according to frontend playback confirmations."""
        del event
        if not self._generated_chunk_ms:
            return
        remaining_ms = max(0.0, self._generated_chunk_ms.popleft())
        self._played_without_segment_ms += remaining_ms
        await self._apply_pending_playback_time()

    def _build_reported_text(self) -> str:
        """Return the current played text prefix across completed and active segments."""

        if not self._segments:
            return self._completed_text
        segment = self._segments[0]
        if not segment.text:
            return self._completed_text
        if segment.total_audio_ms <= 0.0:
            return self._completed_text + segment.text

        # TODO: improve prefix mapping strategy inside a segment
        ratio = max(0.0, min(1.0, segment.played_audio_ms / segment.total_audio_ms))
        prefix_len = min(len(segment.text), int(len(segment.text) * ratio))
        if ratio > 0.0 and prefix_len == 0:
            prefix_len = 1
        return self._completed_text + segment.text[:prefix_len]

    @staticmethod
    def _chunk_duration_ms(*, audio_chunk: bytes, sample_rate: int) -> float:
        """Return PCM duration in milliseconds for one mono int16 chunk."""

        if not audio_chunk or sample_rate <= 0:
            return 0.0
        sample_count = len(audio_chunk) / 2
        return sample_count * 1000.0 / sample_rate

    async def _apply_pending_playback_time(self) -> None:
        """Apply queued played-audio time to known text segments."""

        remaining_ms = max(0.0, self._played_without_segment_ms)
        if remaining_ms <= 0.0:
            return
        while remaining_ms > 0.0 and self._segments:
            segment = self._segments[0]
            if segment.total_audio_ms <= 0.0:
                self._completed_text += segment.text
                self._segments.popleft()
                continue

            unplayed_ms = max(0.0, segment.total_audio_ms - segment.played_audio_ms)
            consume_ms = min(remaining_ms, unplayed_ms)
            segment.played_audio_ms += consume_ms
            remaining_ms -= consume_ms

            if segment.played_audio_ms + 1e-6 >= segment.total_audio_ms:
                self._completed_text += segment.text
                self._segments.popleft()

        self._played_without_segment_ms = remaining_ms
        await self._publish_progress_if_grew()

    async def _publish_progress_if_grew(self) -> None:
        """Publish one response update when the played text prefix grows."""

        played_text = self._build_reported_text()
        if len(played_text) <= len(self._reported_text):
            return
        self._reported_text = played_text
        await self.event_bus.publish(
            ResponseUpdate(
                session_id=self.session_id,
                text=played_text,
            )
        )

    @Manager.event_handler(LLMAgentResponseFinish, priority=20)
    async def _cache_response_finish(self, event: LLMAgentResponseFinish) -> None:
        """Cache the latest generated response until playback finishes."""

        next_text = event.text or ""
        if self._current_response_text and not next_text.startswith(
            self._current_response_text
        ):
            self._reset_all_state()
        self._current_response_text = next_text
        self._pending_text = next_text

    @Manager.event_handler(TTSPlaybackFinished, priority=20)
    async def _publish_response_finish(self, event: TTSPlaybackFinished) -> None:
        """Publish response-finish after frontend playback completion."""

        del event
        if not self._pending_text:
            logger.warning(
                "TTS playback finished without cached response text - session: %s",
                self.session_id,
            )
            self._reset_all_state()
            return
        try:
            if self._reported_text != self._pending_text:
                await self.event_bus.publish(
                    ResponseUpdate(
                        session_id=self.session_id,
                        text=self._pending_text,
                    )
                )
                self._reported_text = self._pending_text
            await self.event_bus.publish(
                ResponseFinish(
                    session_id=self.session_id,
                    text=self._pending_text,
                )
            )
        finally:
            self._reset_all_state()

    @Manager.event_handler(TTSStopped, priority=20)
    async def _handle_tts_stopped(self, event: TTSStopped) -> None:
        """Drop pending playback-tracking state when TTS is stopped early."""

        del event
        self._reset_all_state()

    async def shutdown(self) -> None:
        return
