"""Utilities for asynchronous iteration."""

import sys
import warnings
import functools
from . import compat
from collections.abc import AsyncIterator

try:
    from contextlib import AsyncExitStack
except ImportError:  # pragma: no cover
    from async_exit_stack import AsyncExitStack

__all__ = ['aiter', 'anext', 'await_', 'async_',
           'is_async_iterable', 'assert_async_iterable',
           'is_async_iterator', 'assert_async_iterator',
           'AsyncIteratorContext', 'aitercontext', 'AsyncExitStack']


# Magic method shorcuts

def aiter(obj):
    """Access aiter magic method."""
    assert_async_iterable(obj)
    return obj.__aiter__()


def anext(obj):
    """Access anext magic method."""
    assert_async_iterator(obj)
    return obj.__anext__()


# Async / await helper functions


async def await_(obj):
    """Identity coroutine function."""
    return await obj


def async_(fn):
    """Wrap the given function into a coroutine function."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await fn(*args, **kwargs)
    return wrapper


# Iterability helpers

def is_async_iterable(obj):
    """Check if the given object is an asynchronous iterable."""
    return hasattr(obj, '__aiter__')


def assert_async_iterable(obj):
    """Raise a TypeError if the given object is not an
    asynchronous iterable.
    """
    if not is_async_iterable(obj):
        raise TypeError(
            f"{type(obj).__name__!r} object is not async iterable")


def is_async_iterator(obj):
    """Check if the given object is an asynchronous iterator."""
    return hasattr(obj, '__anext__')


def assert_async_iterator(obj):
    """Raise a TypeError if the given object is not an
    asynchronous iterator.
    """
    if not is_async_iterator(obj):
        raise TypeError(
            f"{type(obj).__name__!r} object is not an async iterator")


# Async iterator context

class AsyncIteratorContext(AsyncIterator):
    """Asynchronous iterator with context management.

    The context management makes sure the aclose asynchronous method
    of the corresponding iterator has run before it exits. It also issues
    warnings and RuntimeError if it is used incorrectly.

    Correct usage::

        ait = some_asynchronous_iterable()
        async with AsyncIteratorContext(ait) as safe_ait:
            async for item in safe_ait:
                <block>

    It is nonetheless not meant to use directly.
    Prefer aitercontext helper instead.
    """

    _STANDBY = "STANDBY"
    _RUNNING = "RUNNING"
    _FINISHED = "FINISHED"
    _BUSY = "BUSY"
    _EXHAUSTED = "EXHAUSTED"

    def __init__(self, aiterator):
        """Initialize with an asynchronous iterator."""
        assert_async_iterator(aiterator)
        if isinstance(aiterator, AsyncIteratorContext):
            raise TypeError(
                f'{aiterator!r} is already an AsyncIteratorContext')
        self._state = self._STANDBY
        self._aiterator = aiterator
        self._task_group = compat.create_task_group()
        self._item_sender, self._item_receiver = compat.open_channel()
        self._sync_sender, self._sync_receiver = compat.open_channel()

    async def _task_target(self):
        # Control the memory channel
        async with self._item_sender:

            # Control aiterator life span
            try:

                # Loop over items, using handshake synchronization
                while True:
                    await self._sync_receiver.receive()

                    # Propagate items
                    try:
                        item = await anext(self._aiterator)
                        await self._item_sender.send((item, None))
                        continue

                    # Stop the iteration
                    except StopAsyncIteration:
                        break

                    # Propagate exceptions
                    except Exception as exc:
                        await self._item_sender.send((None, exc))
                        break

            # Safely terminates aiterator
            finally:

                # Look for an aclose method
                aclose = getattr(self._aiterator, 'aclose', None)

                # The ag_running attribute only exists for python >= 3.8
                running = getattr(self._aiterator, 'ag_running', False)

                # A RuntimeError is raised if aiterator is already running
                if aclose and not running:
                    try:
                        async with compat.open_cancel_scope(shield=True):
                            await aclose()

                    # Work around bpo-35409
                    except GeneratorExit:
                        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Unsafe iteration
        if self._state == self._STANDBY:
            warnings.warn(
                "AsyncIteratorContext is iterated outside of its context",
                stacklevel=2)
            await self.__aenter__()

        # Closed context
        if self._state == self._FINISHED:
            raise RuntimeError(
                "AsyncIteratorContext is closed and cannot be iterated")

        # Iteration is over
        if self._state == self._EXHAUSTED:
            raise StopAsyncIteration

        # Perform a handshake
        if self._state != self._BUSY:
            self._state = self._BUSY
            await self._sync_sender.send(None)

        # Now waits for the next
        try:
            item, exc = await anext(self._item_receiver)

        # The iterator is exhausted
        except StopAsyncIteration:
            self._state = self._EXHAUSTED
            raise

        # An exception has been raised
        if exc is not None:
            self._state = self._EXHAUSTED
            raise exc

        # Return the produced item
        self._state = self._RUNNING
        return item

    async def __aenter__(self):
        if self._state == self._RUNNING:
            raise RuntimeError(
                "AsyncIteratorContext is running and cannot be entered")
        if self._state == self._FINISHED:
            raise RuntimeError(
                "AsyncIteratorContext is closed and cannot be entered")
        self._state = self._RUNNING

        await self._sync_sender.__aenter__()
        await self._task_group.__aenter__()
        await self._task_group.spawn(self._task_target)
        return self

    async def __aexit__(self, typ, value, traceback):
        try:
            if self._state == self._FINISHED:
                return
            try:
                try:
                    if typ in (None, GeneratorExit):
                        await self._task_group.cancel_scope.cancel()
                finally:
                    if typ is GeneratorExit:
                        await self._task_group.__aexit__(None, None, None)
                    else:
                        await self._task_group.__aexit__(typ, value, traceback)
            finally:
                await self._sync_sender.__aexit__(*sys.exc_info())
        finally:
            self._state = self._FINISHED


def aitercontext(aiterable, *, cls=AsyncIteratorContext):
    """Return an asynchronous context manager from an asynchronous iterable.

    The context management makes sure the aclose asynchronous method
    has run before it exits. It also issues warnings and RuntimeError
    if it is used incorrectly.

    It is safe to use with any asynchronous iterable and prevent
    asynchronous iterator context to be wrapped twice.

    Correct usage::

        ait = some_asynchronous_iterable()
        async with aitercontext(ait) as safe_ait:
            async for item in safe_ait:
                <block>

    An optional subclass of AsyncIteratorContext can be provided.
    This class will be used to wrap the given iterable.
    """
    assert issubclass(cls, AsyncIteratorContext)
    aiterator = aiter(aiterable)
    if isinstance(aiterator, cls):
        return aiterator
    return cls(aiterator)
