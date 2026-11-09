from __future__ import annotations

import asyncio
import importlib
import json
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
from .tools import ToolRegistry


class CompositeToolRuntime:
    def __init__(
        self,
        runtimes: Sequence[ToolRuntime] = (),
        *,
        stdio: Sequence[StdioDriver] = (),
    ) -> None:
        self._runtimes = tuple(runtimes)
        self._stdio = tuple(stdio)
        self._owners: dict[str, ToolRuntime] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._started = False
        self._start_lock = asyncio.Lock()
        for runtime in self._runtimes:
            self._add_catalog(runtime)

    @classmethod
    def from_configs(cls, configs: Sequence[ToolConfig]) -> CompositeToolRuntime:
        runtimes: list[ToolRuntime] = []
        stdio: list[StdioDriver] = []
        for config in configs:
            if config.driver == "python":
                if config.target is None:
                    raise HarnessError("Python Tool requires target")
                runtimes.append(load_python_tools(config.target))
            elif config.driver == "stdio":
                stdio.append(StdioDriver(config))
            else:
                raise HarnessError(f"Unsupported Tool driver: {config.driver}")
        return cls(runtimes, stdio=stdio)

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            for driver in self._stdio:
                await driver.start()
                self._add_catalog(driver)
            self._started = True

    async def start_eager(self, configs: Sequence[ToolConfig]) -> None:
        eager = [
            driver
            for driver, config in zip(self._stdio, _stdio_configs(configs), strict=True)
            if config.start == "eager"
        ]
        for driver in eager:
            await driver.start()
            self._add_catalog(driver)

    def catalog(self) -> Sequence[ToolSpec]:
        return tuple(self._specs.values())

    def validate(
        self, calls: Sequence[ToolCall]
    ) -> tuple[tuple[ValidatedCall, ...], tuple[ValidationIssue, ...]]:
        validated = []
        issues = []
        seen_ids: set[str] = set()
        for index, call in enumerate(calls):
            call_id = call.call_id or f"unidentified_call_{index}"
            if not call.call_id:
                issues.append(ValidationIssue(call_id, call.name, "Tool Call identity is empty"))
            elif call.call_id in seen_ids:
                issues.append(
                    ValidationIssue(call_id, call.name, "Tool Call identity is duplicated")
                )
            seen_ids.add(call.call_id)
            spec = self._specs.get(call.name)
            if spec is None:
                issues.append(ValidationIssue(call_id, call.name, "Unknown Tool name"))
                continue
            try:
                arguments = json.loads(call.arguments)
            except (json.JSONDecodeError, TypeError) as exc:
                issues.append(ValidationIssue(call_id, call.name, f"Invalid argument JSON: {exc}"))
                continue
            if not isinstance(arguments, dict):
                issues.append(
                    ValidationIssue(call_id, call.name, "Tool arguments must be a JSON object")
                )
                continue
            validator = jsonschema.Draft202012Validator(spec.parameters)
            for error in sorted(validator.iter_errors(arguments), key=lambda item: list(item.path)):
                path = "$" + "".join(
                    f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.path
                )
                issues.append(ValidationIssue(call_id, call.name, f"{path}: {error.message}"))
            validated.append(ValidatedCall(call=call, spec=spec, arguments=arguments))
        return tuple(validated), tuple(issues)

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
        for driver in self._stdio:
            await driver.close()

    def _add_catalog(self, runtime: ToolRuntime) -> None:
        if any(owner is runtime for owner in self._owners.values()):
            return
        for spec in runtime.catalog():
            if spec.name in self._specs:
                raise HarnessError(f"Duplicate Tool name: {spec.name}")
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
