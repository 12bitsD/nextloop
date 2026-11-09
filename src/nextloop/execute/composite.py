from __future__ import annotations

import asyncio
import importlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

import jsonschema

from ..contracts import (
    HarnessError,
    ToolCall,
    ToolConfig,
    ToolOutcome,
    ToolRuntime,
    ToolSpec,
    ValidatedCall,
    ValidationIssue,
)
from .stdio import StdioDriver
from .tools import ToolRegistry, validate_calls


class CompositeToolRuntime:
    def __init__(
        self,
        runtimes: Sequence[ToolRuntime] = (),
        *,
        stdio: Sequence[StdioDriver] = (),
        origins: Sequence[str] = (),
        stdio_origins: Sequence[str] = (),
    ) -> None:
        self._runtimes = tuple(runtimes)
        self._stdio = tuple(stdio)
        self._origins = tuple(origins) or tuple("Python runtime" for _ in self._runtimes)
        self._stdio_origins = tuple(stdio_origins) or tuple(
            f"stdio worker {' '.join(driver.config.command)}" for driver in self._stdio
        )
        self._owners: dict[str, ToolRuntime] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._started = False
        self._start_lock = asyncio.Lock()
        for runtime, origin in zip(self._runtimes, self._origins, strict=True):
            self._add_catalog(runtime, origin)

    @classmethod
    def from_configs(
        cls, configs: Sequence[ToolConfig], *, sensitive_env: Sequence[str] = ()
    ) -> CompositeToolRuntime:
        runtimes: list[ToolRuntime] = []
        origins: list[str] = []
        stdio: list[StdioDriver] = []
        stdio_origins: list[str] = []
        for config in configs:
            if config.driver == "python":
                if config.target is None:
                    raise HarnessError("Python Tool requires target")
                runtimes.append(load_python_tools(config.target))
                origins.append(f"Python target {config.target}")
            elif config.driver == "stdio":
                stdio.append(StdioDriver(config, sensitive_env=sensitive_env))
                stdio_origins.append(f"stdio worker {' '.join(config.command)}")
            else:
                raise HarnessError(f"Unsupported Tool driver: {config.driver}")
        return cls(
            runtimes,
            stdio=stdio,
            origins=origins,
            stdio_origins=stdio_origins,
        )

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            for driver, origin in zip(self._stdio, self._stdio_origins, strict=True):
                await driver.start()
                self._add_catalog(driver, origin)
            self._started = True

    async def start_eager(self, configs: Sequence[ToolConfig]) -> None:
        eager = [
            (driver, origin)
            for driver, config, origin in zip(
                self._stdio, _stdio_configs(configs), self._stdio_origins, strict=True
            )
            if config.start == "eager"
        ]
        for driver, origin in eager:
            await driver.start()
            self._add_catalog(driver, origin)

    def catalog(self) -> Sequence[ToolSpec]:
        return tuple(self._specs.values())

    def validate(
        self, calls: Sequence[ToolCall]
    ) -> tuple[tuple[ValidatedCall, ...], tuple[ValidationIssue, ...]]:
        return validate_calls(calls, self._specs)

    async def execute(
        self,
        call: ToolCall,
        arguments: Mapping[str, Any],
        *,
        session_id: str,
        turn: int,
        step: int,
        cancel: asyncio.Event,
    ) -> ToolOutcome:
        await self.start()
        runtime = self._owners[call.name]
        return await runtime.execute(
            call,
            arguments,
            session_id=session_id,
            turn=turn,
            step=step,
            cancel=cancel,
        )

    async def close(self) -> None:
        closing = asyncio.create_task(self._close())
        current = asyncio.current_task()
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
                if current is not None:
                    current.uncancel()
        await closing
        if cancelled:
            raise asyncio.CancelledError()

    async def _close(self) -> None:
        for driver in self._stdio:
            await driver.close()

    def _add_catalog(self, runtime: ToolRuntime, origin: str = "Tool runtime") -> None:
        if any(owner is runtime for owner in self._owners.values()):
            return
        for spec in runtime.catalog():
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", spec.name) is None:
                raise HarnessError(
                    f"{origin}: Tool '{spec.name}' has invalid name; expected 1-64 characters "
                    "matching [A-Za-z][A-Za-z0-9_-]*"
                )
            try:
                jsonschema.Draft202012Validator.check_schema(spec.parameters)
            except jsonschema.SchemaError as exc:
                raise HarnessError(
                    f"{origin}: Tool '{spec.name}' has invalid JSON Schema: {exc.message}"
                ) from exc
            if spec.name in self._specs:
                raise HarnessError(f"{origin}: Duplicate Tool name: {spec.name}")
            self._specs[spec.name] = spec
            self._owners[spec.name] = runtime


def load_python_tools(target: str) -> ToolRegistry:
    module_name, separator, register_name = target.partition(":")
    if not separator or not module_name or not register_name:
        raise HarnessError("Python Tool target must have the form module:register")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise HarnessError(f"Cannot import Python Tool module '{module_name}': {exc}") from exc
    register = getattr(module, register_name, None)
    if not callable(register):
        raise HarnessError(f"Python Tool target '{target}' is not callable")
    registry = ToolRegistry()
    try:
        register(registry)
    except Exception as exc:
        raise HarnessError(f"Python Tool registration '{target}' failed: {exc}") from exc
    if not registry.catalog():
        raise HarnessError(f"Python Tool registration '{target}' registered no Tools")
    return registry


def _stdio_configs(configs: Sequence[ToolConfig]) -> tuple[ToolConfig, ...]:
    return tuple(config for config in configs if config.driver == "stdio")
