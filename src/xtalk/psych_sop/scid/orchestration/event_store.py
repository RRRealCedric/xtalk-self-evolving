"""Asynchronous append-only event and latest-wins snapshot persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ....log_utils import logger
from .state_graph import DomainEvent, MAX_EVENT_BYTES


@dataclass(slots=True)
class _FlushBarrier:
    future: asyncio.Future[None]


class EpisodeEventStore:
    """Persist redacted events without blocking the realtime event loop."""

    def __init__(
        self,
        *,
        episode_dir: str | Path,
        episode_id: str,
        queue_capacity: int = 256,
        snapshot_debounce_seconds: float = 0.05,
    ) -> None:
        if type(queue_capacity) is not int or queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        if not isinstance(episode_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", episode_id
        ):
            raise ValueError("episode_id is not a safe file stem")
        self.episode_dir = Path(episode_dir)
        self.episode_id = episode_id
        self.snapshot_debounce_seconds = max(0.0, snapshot_debounce_seconds)
        self._event_queue: asyncio.Queue[DomainEvent | _FlushBarrier | None] = (
            asyncio.Queue(maxsize=queue_capacity)
        )
        self._snapshot_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=1
        )
        self._artifact_queue: asyncio.Queue[dict[str, Any] | _FlushBarrier | None] = (
            asyncio.Queue(maxsize=queue_capacity)
        )
        self._event_writer_task: asyncio.Task[None] | None = None
        self._snapshot_writer_task: asyncio.Task[None] | None = None
        self._artifact_writer_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        self._event_count = 0
        self._last_event_seq = 0
        self._artifact_count = 0
        self._last_snapshot_payload: dict[str, Any] | None = None
        self._write_error: str | None = None

    @property
    def event_log_path(self) -> Path:
        return self.episode_dir / f"{self.episode_id}.events.jsonl"

    @property
    def partial_snapshot_path(self) -> Path:
        return self.episode_dir / f"{self.episode_id}.partial.json"

    @property
    def artifact_log_path(self) -> Path:
        """Return the opt-in raw-text artifact stream path."""

        return self.episode_dir / f"{self.episode_id}.artifacts.jsonl"

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def last_event_seq(self) -> int:
        return self._last_event_seq

    @property
    def write_error(self) -> str | None:
        return self._write_error

    @property
    def closed(self) -> bool:
        """Return whether all persistence workers have been stopped."""

        return self._closed

    @property
    def queue_size(self) -> int:
        return self._event_queue.qsize()

    async def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("EpisodeEventStore is closed")
        await asyncio.to_thread(self._prepare_directory)
        self._event_writer_task = asyncio.create_task(
            self._event_writer(),
            name=f"scid-event-writer-{self.episode_id}",
        )
        self._snapshot_writer_task = asyncio.create_task(
            self._snapshot_writer(),
            name=f"scid-snapshot-writer-{self.episode_id}",
        )
        self._artifact_writer_task = asyncio.create_task(
            self._artifact_writer(),
            name=f"scid-artifact-writer-{self.episode_id}",
        )
        self._started = True

    async def append(self, event: DomainEvent) -> None:
        """Append one domain event or fail loudly if persistence is down."""

        if self._closed:
            raise RuntimeError("EpisodeEventStore is closed")
        if not self._started:
            await self.start()
        if self._event_writer_task is not None and self._event_writer_task.done():
            raise RuntimeError(
                f"SCID event writer is unavailable: {self._write_error or 'stopped'}"
            )
        await self._event_queue.put(event)

    def request_snapshot(self, payload: dict[str, Any]) -> bool:
        """Queue only the latest partial snapshot without waiting for disk."""

        if self._closed or not self._started:
            return False
        self._last_snapshot_payload = payload
        if self._snapshot_queue.full():
            with suppress(asyncio.QueueEmpty):
                self._snapshot_queue.get_nowait()
                self._snapshot_queue.task_done()
        self._snapshot_queue.put_nowait(payload)
        return True

    async def append_artifact(
        self,
        *,
        interaction_seq: int,
        role: str,
        text: str,
    ) -> str:
        """Append one explicitly opted-in raw-text artifact.

        Returns
        -------
        str
            Content-addressed identifier referenced by redacted projections.
        """

        if self._closed:
            raise RuntimeError("EpisodeEventStore is closed")
        if not self._started:
            await self.start()
        if self._artifact_writer_task is not None and self._artifact_writer_task.done():
            raise RuntimeError(
                "SCID artifact writer is unavailable: "
                f"{self._write_error or 'stopped'}"
            )
        artifact_id = _artifact_id(text)
        await self._artifact_queue.put(
            {
                "schema_version": 1,
                "artifact_id": artifact_id,
                "interaction_seq": interaction_seq,
                "role": role,
                "text": text,
            }
        )
        return artifact_id

    async def flush(self) -> None:
        """Drain queued events and durably flush the latest projection.

        Raises
        ------
        RuntimeError
            If the event writer has already failed.
        """

        if not self._started or self._closed:
            return
        if self._event_writer_task is not None and self._event_writer_task.done():
            raise RuntimeError(
                f"SCID event writer is unavailable: {self._write_error or 'stopped'}"
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        await self._event_queue.put(_FlushBarrier(future=future))
        await future
        if self._artifact_writer_task is not None and self._artifact_writer_task.done():
            raise RuntimeError(
                f"SCID artifact writer is unavailable: {self._write_error or 'stopped'}"
            )
        artifact_future: asyncio.Future[None] = loop.create_future()
        await self._artifact_queue.put(_FlushBarrier(future=artifact_future))
        await artifact_future
        payload = self._last_snapshot_payload
        if payload is not None:
            await asyncio.to_thread(
                self._atomic_write_json,
                self.partial_snapshot_path,
                payload,
            )

    async def write_final(
        self,
        *,
        path: Path,
        payload: dict[str, Any],
    ) -> None:
        """Durably write the final snapshot after draining domain events."""

        if self._closed:
            await asyncio.to_thread(self._atomic_write_json, path, payload)
            return
        if not self._started:
            await self.start()
        await self.flush()
        await asyncio.to_thread(self._atomic_write_json, path, payload)

    async def read_events(
        self,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if self._started and not self._closed:
            await self.flush()
        return await asyncio.to_thread(
            self._read_events_sync,
            self.event_log_path,
            after_seq,
            limit,
        )

    async def close(self) -> None:
        """Drain and stop all background projection writers.

        Writer shutdown is best-effort even when a durability barrier fails.
        The original persistence error is re-raised after every live worker has
        been stopped so callers never trade an observable flush failure for an
        orphan writer task.
        """

        if self._closed:
            return
        if not self._started:
            self._closed = True
            return
        flush_error: Exception | None = None
        try:
            await self.flush()
        except Exception as exc:
            flush_error = exc
        self._closed = True
        if self._event_writer_task is not None and not self._event_writer_task.done():
            await self._event_queue.put(None)
        if (
            self._snapshot_writer_task is not None
            and not self._snapshot_writer_task.done()
        ):
            if self._snapshot_queue.full():
                with suppress(asyncio.QueueEmpty):
                    self._snapshot_queue.get_nowait()
                    self._snapshot_queue.task_done()
            self._snapshot_queue.put_nowait(None)
        if (
            self._artifact_writer_task is not None
            and not self._artifact_writer_task.done()
        ):
            await self._artifact_queue.put(None)
        tasks = [
            task
            for task in (
                self._event_writer_task,
                self._snapshot_writer_task,
                self._artifact_writer_task,
            )
            if task is not None
        ]
        writer_errors: list[BaseException] = []
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            writer_errors = [
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            ]
        self._event_writer_task = None
        self._snapshot_writer_task = None
        self._artifact_writer_task = None
        if flush_error is not None:
            raise flush_error
        if writer_errors:
            raise RuntimeError(
                "SCID persistence writer failed during shutdown"
            ) from writer_errors[0]

    def snapshot(self) -> dict[str, Any]:
        """Return bounded event-store health and position metadata."""

        return {
            "schema_version": 1,
            "file_name": self.event_log_path.name,
            "event_count": self._event_count,
            "last_event_seq": self._last_event_seq,
            "queue_size": self.queue_size,
            "writer_active": bool(
                self._event_writer_task is not None
                and not self._event_writer_task.done()
            ),
            "snapshot_writer_active": bool(
                self._snapshot_writer_task is not None
                and not self._snapshot_writer_task.done()
            ),
            "artifact_file_name": (
                self.artifact_log_path.name if self._artifact_count else None
            ),
            "artifact_count": self._artifact_count,
            "artifact_writer_active": bool(
                self._artifact_writer_task is not None
                and not self._artifact_writer_task.done()
            ),
            "write_error": self._write_error,
        }

    async def _event_writer(self) -> None:
        pending_barriers: list[_FlushBarrier] = []
        try:
            while True:
                item = await self._event_queue.get()
                if item is None:
                    self._event_queue.task_done()
                    return
                if isinstance(item, _FlushBarrier):
                    pending_barriers = [item]
                    await asyncio.to_thread(self._fsync_event_log)
                    if not item.future.done():
                        item.future.set_result(None)
                    pending_barriers = []
                    self._event_queue.task_done()
                    continue

                batch = [item]
                barriers: list[_FlushBarrier] = []
                stop_after_batch = False
                while len(batch) < 32:
                    try:
                        queued = self._event_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if queued is None:
                        stop_after_batch = True
                        self._event_queue.task_done()
                        break
                    if isinstance(queued, _FlushBarrier):
                        barriers.append(queued)
                        self._event_queue.task_done()
                        break
                    batch.append(queued)

                pending_barriers = barriers
                await asyncio.to_thread(self._append_event_batch, batch)
                for _ in batch:
                    self._event_queue.task_done()
                if barriers:
                    await asyncio.to_thread(self._fsync_event_log)
                    for barrier in barriers:
                        if not barrier.future.done():
                            barrier.future.set_result(None)
                    pending_barriers = []
                if stop_after_batch:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._write_error = type(exc).__name__
            logger.exception(
                "SCID event writer failed - episode: %s",
                self.episode_id,
            )
            for barrier in pending_barriers:
                if not barrier.future.done():
                    barrier.future.set_exception(exc)
            while True:
                try:
                    pending = self._event_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if isinstance(pending, _FlushBarrier) and not pending.future.done():
                    pending.future.set_exception(exc)
                self._event_queue.task_done()

    async def _snapshot_writer(self) -> None:
        try:
            while True:
                payload = await self._snapshot_queue.get()
                if payload is None:
                    self._snapshot_queue.task_done()
                    return
                if self.snapshot_debounce_seconds:
                    await asyncio.sleep(self.snapshot_debounce_seconds)
                while True:
                    try:
                        newer = self._snapshot_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    self._snapshot_queue.task_done()
                    if newer is None:
                        return
                    payload = newer
                await asyncio.to_thread(
                    self._atomic_write_json,
                    self.partial_snapshot_path,
                    payload,
                )
                self._snapshot_queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._write_error = type(exc).__name__
            logger.exception(
                "SCID snapshot writer failed - episode: %s",
                self.episode_id,
            )

    async def _artifact_writer(self) -> None:
        pending_barrier: _FlushBarrier | None = None
        try:
            while True:
                item = await self._artifact_queue.get()
                if item is None:
                    self._artifact_queue.task_done()
                    return
                if isinstance(item, _FlushBarrier):
                    pending_barrier = item
                    await asyncio.to_thread(self._fsync_artifact_log)
                    if not item.future.done():
                        item.future.set_result(None)
                    pending_barrier = None
                    self._artifact_queue.task_done()
                    continue
                await asyncio.to_thread(self._append_artifact_record, item)
                self._artifact_queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._write_error = type(exc).__name__
            logger.exception(
                "SCID artifact writer failed - episode: %s",
                self.episode_id,
            )
            if pending_barrier is not None and not pending_barrier.future.done():
                pending_barrier.future.set_exception(exc)
            while True:
                try:
                    pending = self._artifact_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if isinstance(pending, _FlushBarrier) and not pending.future.done():
                    pending.future.set_exception(exc)
                self._artifact_queue.task_done()

    def _prepare_directory(self) -> None:
        self.episode_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.episode_dir, 0o700)
        if not self.event_log_path.exists():
            fd = os.open(
                self.event_log_path,
                os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                0o600,
            )
            os.close(fd)
        os.chmod(self.event_log_path, 0o600)

    def _append_event_batch(self, events: list[DomainEvent]) -> None:
        self._prepare_directory()
        encoded: list[bytes] = []
        for event in events:
            raw = json.dumps(
                event.snapshot(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(raw) > MAX_EVENT_BYTES:
                raise ValueError(f"SCID domain event exceeds {MAX_EVENT_BYTES} bytes")
            encoded.append(raw + b"\n")
        with self.event_log_path.open("ab") as handle:
            for raw in encoded:
                handle.write(raw)
            handle.flush()
        self._event_count += len(events)
        if events:
            self._last_event_seq = events[-1].event_seq

    def _fsync_event_log(self) -> None:
        self._prepare_directory()
        with self.event_log_path.open("ab") as handle:
            handle.flush()
            os.fsync(handle.fileno())

    def _append_artifact_record(self, payload: dict[str, Any]) -> None:
        self._prepare_directory()
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(raw) > MAX_EVENT_BYTES:
            raise ValueError("SCID text artifact exceeds 64 KiB")
        with self.artifact_log_path.open("ab") as handle:
            handle.write(raw + b"\n")
            handle.flush()
        os.chmod(self.artifact_log_path, 0o600)
        self._artifact_count += 1

    def _fsync_artifact_log(self) -> None:
        if not self.artifact_log_path.exists():
            return
        with self.artifact_log_path.open("ab") as handle:
            handle.flush()
            os.fsync(handle.fileno())

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self._prepare_directory()
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.episode_id}.",
            suffix=".tmp",
            dir=self.episode_dir,
        )
        temporary_path = Path(temporary_name)
        replaced = False
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            replaced = True
            try:
                directory_fd = os.open(self.episode_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                logger.warning(
                    "SCID snapshot directory fsync failed - path: %s",
                    path,
                )
        finally:
            if not replaced:
                with suppress(OSError):
                    os.close(fd)
                with suppress(FileNotFoundError):
                    temporary_path.unlink()

    @staticmethod
    def _read_events_sync(
        path: Path,
        after_seq: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                event_seq = payload.get("event_seq")
                if not isinstance(event_seq, int) or event_seq <= after_seq:
                    continue
                events.append(payload)
                if len(events) >= limit:
                    break
        return events


def _artifact_id(text: str) -> str:
    return f"text_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"
