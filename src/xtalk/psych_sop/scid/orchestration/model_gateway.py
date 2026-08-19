"""Deadline-aware admission and resilience for SCID model requests."""

from __future__ import annotations

import asyncio
import heapq
import math
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, TypeVar

from ..state.telemetry import monotonic_ts


T = TypeVar("T")


class ModelPriority(IntEnum):
    """Model admission priority, with lower values admitted first."""

    CRISIS_OR_FOREGROUND = 0
    ASSESSOR = 10
    FINAL_OBSERVER = 20
    PARTIAL_OBSERVER = 30
    CANDIDATE_GENERATION = 40


@dataclass(slots=True)
class _Waiter:
    priority: int
    order: int
    future: asyncio.Future[None]


class _PriorityLimiter:
    """Small FIFO-within-priority async concurrency limiter."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._active = 0
        self._order = 0
        self._waiters: list[tuple[int, int, _Waiter]] = []
        self._lock = asyncio.Lock()

    async def acquire(self, priority: ModelPriority, deadline: float) -> None:
        """Acquire capacity before the monotonic deadline."""

        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._active < self.capacity and not self._waiters:
                self._active += 1
                return
            self._order += 1
            waiter = _Waiter(
                priority=int(priority),
                order=self._order,
                future=loop.create_future(),
            )
            heapq.heappush(
                self._waiters,
                (waiter.priority, waiter.order, waiter),
            )
        try:
            remaining = deadline - monotonic_ts()
            if remaining <= 0:
                raise TimeoutError("model admission deadline expired")
            await asyncio.wait_for(waiter.future, timeout=remaining)
        except BaseException:
            async with self._lock:
                if not waiter.future.done():
                    waiter.future.cancel()
            raise

    async def release(self) -> None:
        """Release capacity and admit the next live waiter."""

        async with self._lock:
            self._active = max(0, self._active - 1)
            while self._waiters and self._active < self.capacity:
                _, _, waiter = heapq.heappop(self._waiters)
                if waiter.future.done():
                    continue
                self._active += 1
                waiter.future.set_result(None)
                break


_GLOBAL_LIMITERS: (
    "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _PriorityLimiter]"
) = weakref.WeakKeyDictionary()


class ModelGateway:
    """Gate model requests by priority, deadline, retry, and circuit state."""

    def __init__(
        self,
        *,
        per_session_concurrency: int = 4,
        global_concurrency: int = 16,
        failure_threshold: int = 4,
        recovery_seconds: float = 10.0,
    ) -> None:
        if per_session_concurrency < 1 or global_concurrency < 1:
            raise ValueError("model concurrency limits must be positive")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if not math.isfinite(recovery_seconds) or recovery_seconds <= 0:
            raise ValueError("recovery_seconds must be positive and finite")
        self._session_limiter = _PriorityLimiter(per_session_concurrency)
        self._global_capacity = global_concurrency
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._consecutive_failures = 0
        self._circuit_opened_at: float | None = None
        self._request_count = 0
        self._rejected_count = 0
        self._timeout_count = 0
        # Request identity is operational metadata, not model content. Keeping
        # the latest value makes gateway health snapshots traceable without
        # retaining prompts or responses.
        self._last_request_id: str | None = None

    async def run(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        priority: ModelPriority,
        deadline_monotonic: float,
        request_id: str,
        retries: int = 0,
    ) -> T:
        """Execute one admitted request within its absolute deadline.

        Parameters
        ----------
        operation : Callable[[], Awaitable[T]]
            Factory creating a fresh awaitable for each retry.
        priority : ModelPriority
            Admission priority for this request.
        deadline_monotonic : float
            Absolute monotonic deadline shared by the turn.
        request_id : str
            Stable request identity used by callers for causal tracing.
        retries : int, optional
            Number of bounded retries after the first attempt.

        Returns
        -------
        T
            Model result.
        """

        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        self._last_request_id = request_id
        now = monotonic_ts()
        if (
            self._circuit_opened_at is not None
            and now - self._circuit_opened_at < self.recovery_seconds
        ):
            self._rejected_count += 1
            raise RuntimeError("model gateway circuit is open")
        if self._circuit_opened_at is not None:
            self._circuit_opened_at = None
            self._consecutive_failures = 0

        global_limiter = self._global_limiter()
        await global_limiter.acquire(priority, deadline_monotonic)
        try:
            await self._session_limiter.acquire(priority, deadline_monotonic)
            try:
                self._request_count += 1
                last_error: BaseException | None = None
                for attempt in range(retries + 1):
                    remaining = deadline_monotonic - monotonic_ts()
                    if remaining <= 0:
                        self._timeout_count += 1
                        raise TimeoutError("model request deadline expired")
                    try:
                        result = await asyncio.wait_for(
                            operation(),
                            timeout=remaining,
                        )
                    except asyncio.CancelledError:
                        raise
                    except (TimeoutError, asyncio.TimeoutError) as exc:
                        self._timeout_count += 1
                        last_error = exc
                    except Exception as exc:
                        last_error = exc
                    else:
                        self._consecutive_failures = 0
                        return result
                    if attempt >= retries:
                        break
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failure_threshold:
                    self._circuit_opened_at = monotonic_ts()
                if last_error is None:
                    raise RuntimeError("model request failed without an error")
                raise last_error
            finally:
                await self._session_limiter.release()
        finally:
            await global_limiter.release()

    def snapshot(self) -> dict[str, Any]:
        """Return bounded health counters without model payloads."""

        return {
            "request_count": self._request_count,
            "rejected_count": self._rejected_count,
            "timeout_count": self._timeout_count,
            "last_request_id": self._last_request_id,
            "consecutive_failures": self._consecutive_failures,
            "circuit_open": self._circuit_opened_at is not None,
        }

    def _global_limiter(self) -> _PriorityLimiter:
        loop = asyncio.get_running_loop()
        limiter = _GLOBAL_LIMITERS.get(loop)
        if limiter is None:
            limiter = _PriorityLimiter(self._global_capacity)
            _GLOBAL_LIMITERS[loop] = limiter
        return limiter
