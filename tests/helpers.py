from __future__ import annotations

import inspect

from nextloop.adapters.memory import InMemoryEventStore
from nextloop.contracts import LoopConfig


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.attempts = []

    async def complete(self, request, *, attempt, cancel):
        self.requests.append(request)
        self.attempts.append((attempt, request.canonical_bytes()))
        if not self.responses:
            raise AssertionError("FakeProvider has no queued response")
        value = self.responses.pop(0)
        if callable(value):
            value = value(request=request, attempt=attempt, cancel=cancel)
            if inspect.isawaitable(value):
                value = await value
        if isinstance(value, BaseException):
            raise value
        return value


class FakeDelivery:
    def __init__(self, failures=0):
        self.failures = failures
        self.messages = []

    async def deliver(self, message):
        self.messages.append(message)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("delivery receipt unavailable")


def config(**kwargs):
    kwargs.setdefault("system_prompt", "system")
    kwargs.setdefault("retry_backoff_seconds", 0)
    return LoopConfig(**kwargs)


class CountingEventStore(InMemoryEventStore):
    def __init__(self):
        super().__init__()
        self.recovery_reads = 0

    async def load_recovery(self, session_id):
        self.recovery_reads += 1
        return await super().load_recovery(session_id)


class MinimalEventStore(CountingEventStore):
    def __getattribute__(self, name):
        if name in ("get_event", "last_seq"):
            raise AttributeError(name)
        return super().__getattribute__(name)
