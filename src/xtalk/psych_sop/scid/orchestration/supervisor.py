"""Structured per-turn concurrency using AnyIO cancel scopes."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

import anyio


T = TypeVar("T")


class SupervisedTask(Generic[T]):
    """Task-compatible result handle for one supervised child operation."""

    def __init__(self, *, name: str, future: asyncio.Future[T]) -> None:
        self.name = name
        self._future = future
        self._scope: anyio.CancelScope | None = None
        self._cancel_requested = False
        self._report_cancelled = False
        self._started = asyncio.Event()

    def bind_scope(self, scope: anyio.CancelScope) -> None:
        """Bind the AnyIO cancellation scope that owns the child."""

        self._scope = scope
        if self._cancel_requested:
            scope.cancel()

    def cancel(self) -> bool:
        """Cancel the child and its public result future."""

        if self.done():
            return False
        self._cancel_requested = True
        self._report_cancelled = True
        if self._scope is not None:
            self._scope.cancel()
        return True

    def cancel_from_supervisor(self) -> bool:
        """Cancel work while allowing a child to return a cleanup result."""

        if self.done():
            return False
        self._cancel_requested = True
        if self._scope is not None:
            self._scope.cancel()
        return True

    def done(self) -> bool:
        """Return whether the child has reached a terminal state."""

        return self._future.done()

    def cancelled(self) -> bool:
        """Return whether the public result was cancelled."""

        return self._future.cancelled()

    def result(self) -> T:
        """Return the completed child result."""

        return self._future.result()

    def exception(self) -> BaseException | None:
        """Return the completed child exception, if any."""

        return self._future.exception()

    def as_future(self) -> asyncio.Future[T]:
        """Return the underlying Future for asyncio compatibility."""

        return self._future

    async def wait_started(self) -> None:
        """Wait until the supervisor has entered this child's scope."""

        await self._started.wait()

    def add_done_callback(
        self,
        callback: Callable[["SupervisedTask[T]"], Any],
    ) -> None:
        """Register a callback invoked with this handle on completion."""

        self._future.add_done_callback(lambda _: callback(self))

    def __await__(self):  # type: ignore[no-untyped-def]
        return self._future.__await__()


@dataclass(slots=True)
class _SpawnCommand(Generic[T]):
    handle: SupervisedTask[T]
    awaitable: Awaitable[T]


class TurnSupervisor:
    """Own every asynchronous child associated with one interaction."""

    def __init__(
        self,
        *,
        interaction_seq: int,
        deadline_monotonic: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interaction_seq = interaction_seq
        self.deadline_monotonic = deadline_monotonic
        self._clock = clock
        self._commands: asyncio.Queue[_SpawnCommand[Any] | None] = asyncio.Queue()
        self._runner_task: asyncio.Task[None] | None = None
        self._root_scope: anyio.CancelScope | None = None
        self._tasks: set[SupervisedTask[Any]] = set()
        self._ready = asyncio.Event()
        self._started = False
        self._closing = False
        self._closed = False

    async def start(self) -> "TurnSupervisor":
        """Start the owner task and wait until its AnyIO group is ready."""

        if self._started:
            return self
        if self._closed:
            raise RuntimeError("TurnSupervisor is closed")
        self._runner_task = asyncio.create_task(
            self._run(),
            name=f"scid-turn-supervisor-{self.interaction_seq}",
        )
        self._started = True
        await self._ready.wait()
        return self

    def spawn(
        self,
        awaitable: Awaitable[T],
        *,
        name: str,
    ) -> SupervisedTask[T]:
        """Schedule one child under the turn deadline and root scope."""

        if not self._started or self._closing or self._closed:
            _close_awaitable(awaitable)
            raise RuntimeError("TurnSupervisor is not accepting work")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        handle = SupervisedTask(name=name, future=future)
        self._tasks.add(handle)
        handle.add_done_callback(self._tasks.discard)
        self._commands.put_nowait(_SpawnCommand(handle=handle, awaitable=awaitable))
        return handle

    def cancel(self) -> None:
        """Cancel the complete turn scope, including not-yet-started work."""

        if self._closing or self._closed:
            return
        self._closing = True
        for task in list(self._tasks):
            task.cancel_from_supervisor()
        if self._root_scope is not None:
            self._root_scope.cancel()
        self._commands.put_nowait(None)

    @property
    def active_count(self) -> int:
        """Return the number of children that have not completed."""

        return sum(not task.done() for task in self._tasks)

    async def close(self) -> None:
        """Cancel the scope and wait for every child to terminate."""

        if self._closed:
            return
        self.cancel()
        if self._runner_task is not None:
            await asyncio.gather(self._runner_task, return_exceptions=True)
        self._tasks.clear()
        self._closed = True
        self._started = False
        self._runner_task = None
        self._root_scope = None

    async def _run(self) -> None:
        try:
            async with anyio.create_task_group() as task_group:
                self._root_scope = task_group.cancel_scope
                self._ready.set()
                while True:
                    command = await self._commands.get()
                    if command is None:
                        task_group.cancel_scope.cancel()
                        return
                    if command.handle.cancelled():
                        _close_awaitable(command.awaitable)
                        continue
                    task_group.start_soon(
                        self._run_child,
                        command.handle,
                        command.awaitable,
                        name=command.handle.name,
                    )
        finally:
            self._ready.set()
            while not self._commands.empty():
                command = self._commands.get_nowait()
                if command is not None:
                    _close_awaitable(command.awaitable)
                    command.handle.cancel()

    async def _run_child(
        self,
        handle: SupervisedTask[T],
        awaitable: Awaitable[T],
    ) -> None:
        # The deadline is created with ``time.monotonic()`` by the runtime.
        # Consume it with the same clock domain.  ``anyio.current_time()`` is
        # backend-defined and can use a different epoch in embedded/custom
        # event-loop contexts, which previously made fresh turns expire before
        # their first child started.
        now = self._clock()
        remaining = self.deadline_monotonic - now
        missing = object()
        result: T | object = missing
        try:
            with anyio.CancelScope() as scope:
                handle.bind_scope(scope)
                handle._started.set()
                if remaining <= 0:
                    raise TimeoutError(
                        "turn deadline expired before child start: "
                        f"seq={self.interaction_seq}, task={handle.name}, "
                        f"deadline={self.deadline_monotonic:.6f}, "
                        f"now={now:.6f}, remaining={remaining:.6f}"
                    )
                try:
                    with anyio.fail_after(remaining):
                        result = await awaitable
                except asyncio.CancelledError:
                    pass
            if handle.as_future().done():
                return
            if result is missing or handle._report_cancelled:
                handle.as_future().cancel()
            else:
                handle.as_future().set_result(result)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            if not handle.as_future().done():
                handle.as_future().cancel()
            return
        except BaseException as exc:
            if not handle.as_future().done():
                handle.as_future().set_exception(exc)

    async def __aenter__(self) -> "TurnSupervisor":
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        await self.close()


def _close_awaitable(awaitable: Awaitable[Any]) -> None:
    """Close an unstarted coroutine to avoid resource warnings."""

    if inspect.iscoroutine(awaitable):
        awaitable.close()
