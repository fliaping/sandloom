"""Cancellation-safe completion of blocking work that owns runtime state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def complete_in_thread(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Drain the actual worker before propagating cancellation (not rollback).

    A cancelled to_thread await does not stop its thread. Keep the operation
    alive, including through repeated cancellation, so callers cannot tear
    down state while the worker is still using it.
    """
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    return await complete_before_cancelling(worker)


async def complete_before_cancelling(worker: asyncio.Task[T]) -> T:
    """Let all waiters observe completion without cancelling shared work."""
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            if not cancelled:
                raise
            break
    if cancelled:
        # Retrieve exceptions to avoid an unhandled task; caller cancellation
        # retains precedence after the real operation has completed.
        if not worker.cancelled():
            worker.exception()
        raise asyncio.CancelledError
    return worker.result()
