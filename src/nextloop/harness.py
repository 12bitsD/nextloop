from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn, cast

from .adapters.anthropic import AnthropicProvider
from .adapters.memory import InMemoryEventStore
from .adapters.observing import ObservingEventStore, StdoutExporter, _get_turn, _query_history
from .adapters.openai import OpenAIProvider
from .adapters.sqlite import SQLiteEventStore
from .contracts import (
    API_VERSION,
    HARNESS_SCHEMA,
    DeliveryOwner,
    EventStore,
    FinalMessage,
    HarnessError,
    HarnessSpec,
    LoopConfig,
    ModelProvider,
    ProviderConfig,
    RecoveryOutcome,
    SkillConfig,
    StoreConfig,
    ToolConfig,
    ToolExecutionMode,
    ToolRuntime,
    ToolSpec,
    TraceConfig,
    TraceExporterInput,
    TraceRecord,
    TurnContext,
    TurnOutcome,
    TurnTrace,
    is_positive_number,
    new_id,
    parse_version,
)
from .execute.cleanup import wait_for_cleanup
from .execute.composite import CompositeToolRuntime
from .execute.result_read import with_result_reads
from .execute.tools import ToolRegistry
from .loop import AgentLoop
from .skills import SkillRegistry, skill_view

_ROOT_FIELDS = {"schema", "min_nextloop", "provider", "store", "loop", "tools", "skills", "trace"}
_TRACE_FIELDS = {"exporter"}
_PROVIDER_FIELDS = {
    "protocol",
    "model_id",
    "base_url",
    "api_key_env",
    "default_parameters",
    "timeout_seconds",
}
_STORE_FIELDS = {"driver", "path"}
_LOOP_FIELDS = {
    "system_prompt",
    "compaction_prompt",
    "context_window",
    "output_reserve",
    "max_parallel_tools",
    "max_protocol_corrections",
    "max_provider_attempts",
    "retry_backoff_seconds",
    "pacing_step_interval",
    "max_steps",
    "max_model_calls",
    "max_tool_calls",
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "turn_deadline_seconds",
    "tool_result_bytes",
    "tool_result_full_max_bytes",
}
_TOOL_FIELDS = {
    "driver",
    "target",
    "command",
    "namespace",
    "start",
    "resources",
    "timeout_seconds",
    "cancel_grace_seconds",
}
_SKILL_FIELDS = {"paths"}


class MemoryDeliveryOwner:
    def __init__(self) -> None:
        self.messages: list[FinalMessage] = []

    async def deliver(self, message: FinalMessage) -> None:
        self.messages.append(message)


class Harness:
    def __init__(
        self,
        spec: HarnessSpec,
        *,
        session_id: str | None = None,
        exporter: TraceExporterInput | Sequence[TraceExporterInput] | None = None,
        provider: ModelProvider | None = None,
        store: EventStore | None = None,
        delivery: DeliveryOwner | None = None,
        tools: ToolRuntime | None = None,
    ) -> None:
        self.spec = spec
        self.session_id = session_id or new_id("session")
        self._provider = provider
        self._owns_tools = tools is None
        self.tools = (
            CompositeToolRuntime.from_configs(
                spec.tools, sensitive_env=(spec.provider.api_key_env,)
            )
            if tools is None
            else tools
        )
        self._owns_store = store is None
        base_store = _make_store(spec.store) if store is None else store
        exporters = _resolve_exporters(spec.trace.exporter, exporter)
        self.store = (
            ObservingEventStore(
                base_store,
                exporters,
                close_store=self._owns_store,
                close_exporters=exporters[:1] if spec.trace.exporter else (),
            )
            if exporters
            else base_store
        )
        self._close_store_on_close = self._owns_store or bool(exporters)
        self.delivery = MemoryDeliveryOwner() if delivery is None else delivery
        self.loop = AgentLoop()
        self.skills: SkillRegistry | None = None
        self._skill_registry: ToolRegistry | None = None
        self._all_tools: ToolRuntime | None = None

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        session_id: str | None = None,
        exporter: TraceExporterInput | Sequence[TraceExporterInput] | None = None,
        provider: ModelProvider | None = None,
        store: EventStore | None = None,
        delivery: DeliveryOwner | None = None,
        tools: ToolRuntime | None = None,
    ) -> Harness:
        return cls(
            load_harness_spec(path),
            session_id=session_id,
            exporter=exporter,
            provider=provider,
            store=store,
            delivery=delivery,
            tools=tools,
        )

    async def check(self) -> dict[str, Any]:
        if isinstance(self.tools, CompositeToolRuntime):
            await self.tools.start_eager(self.spec.tools)
            await self.tools.start()
        runtime = self._runtime_with_skills()
        return {
            "ok": True,
            "schema": self.spec.schema,
            "nextloop": API_VERSION,
            "tools": [spec.name for spec in runtime.catalog()],
            "skills": [spec.name for spec in self.skills.list()] if self.skills else [],
        }

    async def run(self, initial_input: str) -> TurnOutcome:
        await self.check()
        provider = self._make_provider()
        assert self._all_tools is not None
        context = TurnContext(
            session_id=self.session_id,
            initial_input=initial_input,
            config=self._loop_config(),
            store=self.store,
            provider=provider,
            tools=self._all_tools,
            delivery=self.delivery,
        )
        return await self.loop.run(context)

    async def steer(self, content: str, *, input_id: str | None = None) -> bool:
        return await self.loop.steer(self.session_id, content, input_id=input_id)

    async def cancel(self, cause: str = "user_stop") -> bool:
        return await self.loop.cancel(self.session_id, cause)

    async def recover(self) -> RecoveryOutcome:
        return await self.loop.recover(self.session_id, self.store, self.delivery)

    async def history(
        self, *, from_turn: int = 1, to_turn: int | None = None
    ) -> tuple[TraceRecord, ...]:
        return await _query_history(
            self.store, self.session_id, from_turn=from_turn, to_turn=to_turn
        )

    async def get_turn(self, turn: int) -> TurnTrace | None:
        return await _get_turn(self.store, self.session_id, turn)

    async def close(self) -> None:
        cancelled = await wait_for_cleanup(asyncio.create_task(self._close()))
        if cancelled:
            raise asyncio.CancelledError()

    async def _close(self) -> None:
        try:
            if self._owns_tools and isinstance(self.tools, CompositeToolRuntime):
                await self.tools.close()
        finally:
            close = getattr(self.store, "close", None) if self._close_store_on_close else None
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result

    def _make_provider(self) -> ModelProvider:
        if self._provider is not None:
            return self._provider
        config = self.spec.provider
        if config.protocol not in ("openai", "anthropic"):
            raise HarnessError(f"Unsupported Provider protocol: {config.protocol}")
        api_key = os.environ.get(config.api_key_env)
        if not api_key:
            raise HarnessError(
                f"{self.spec.source_path}: provider.api_key_env environment variable "
                f"'{config.api_key_env}' is not set"
            )
        if config.protocol == "openai":
            return OpenAIProvider(
                model_id=config.model_id,
                base_url=config.base_url,
                api_key=api_key,
                default_parameters=config.default_parameters,
                timeout_seconds=config.timeout_seconds,
            )
        return AnthropicProvider(
            model_id=config.model_id,
            base_url=config.base_url,
            api_key=api_key,
            default_parameters=config.default_parameters,
            timeout_seconds=config.timeout_seconds,
        )

    def _runtime_with_skills(self) -> ToolRuntime:
        if self._all_tools is not None:
            return self._all_tools
        paths = self.spec.skills.paths
        self.skills = SkillRegistry(
            paths, available_tools=[item.name for item in self.tools.catalog()]
        )
        if not paths:
            runtime = self.tools
        else:
            skill_tools = ToolRegistry()
            skill_tools.register(
                ToolSpec(
                    "skill_view",
                    "Load the instructions for an available Skill",
                    {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                    mode=ToolExecutionMode.PARALLEL,
                ),
                skill_view(self.skills),
            )
            self._skill_registry = skill_tools
            runtime = CompositeToolRuntime([self.tools, skill_tools])
        self._all_tools = with_result_reads(runtime, self.store, self.spec.loop.tool_result_bytes)
        return self._all_tools

    def _loop_config(self) -> LoopConfig:
        if not self.skills:
            return self.spec.loop
        metadata = "\n".join(f"- {skill.name}: {skill.description}" for skill in self.skills.list())
        prompt = (
            self.spec.loop.system_prompt
            + "\n\nAvailable Skills (use skill_view to load instructions on demand):\n"
            + metadata
        )
        return replace(self.spec.loop, system_prompt=prompt)


def load_harness_spec(path: str | Path) -> HarnessSpec:
    source = Path(path).expanduser().resolve()
    try:
        with source.open("rb") as stream:
            raw = tomllib.load(stream)
    except OSError as exc:
        raise HarnessError(f"{source}: cannot read configuration: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise HarnessError(f"{source}: invalid TOML: {exc}") from exc
    _fields(raw, _ROOT_FIELDS, "", source)
    _reject_secrets(raw, source)
    schema = _string(raw, "schema", source)
    if schema != HARNESS_SCHEMA:
        _fail(source, "schema", f"expected '{HARNESS_SCHEMA}', got '{schema}'")
    minimum = _string(raw, "min_nextloop", source)
    if re.fullmatch(r"\d+\.\d+\.\d+(?:rc\d+)?", minimum) is None:
        _fail(source, "min_nextloop", "must use full x.y.z or x.y.zrcN form")
    if _version_tuple(API_VERSION, source, "min_nextloop") < _version_tuple(
        minimum, source, "min_nextloop"
    ):
        _fail(source, "min_nextloop", f"requires nextloop >= {minimum}, current is {API_VERSION}")

    provider_raw = _table(raw, "provider", source)
    _fields(provider_raw, _PROVIDER_FIELDS, "provider", source)
    provider = ProviderConfig(
        protocol=_choice(provider_raw, "protocol", ("openai", "anthropic"), source),
        model_id=_string(provider_raw, "model_id", source, "provider"),
        base_url=_string(provider_raw, "base_url", source, "provider"),
        api_key_env=_string(provider_raw, "api_key_env", source, "provider"),
        default_parameters=_optional_table(provider_raw, "default_parameters", source, "provider"),
        timeout_seconds=_number(provider_raw, "timeout_seconds", 60.0, source, "provider"),
    )
    store_raw = _optional_table(raw, "store", source)
    _fields(store_raw, _STORE_FIELDS, "store", source)
    store = StoreConfig(
        driver=_choice(store_raw, "driver", ("memory", "sqlite"), source, default="memory"),
        path=_string(store_raw, "path", source, "store", default=":memory:"),
    )
    if store.driver == "memory" and store.path != ":memory:":
        _fail(source, "store.path", "memory Store only supports ':memory:'")
    if store.driver == "sqlite":
        store = StoreConfig(driver="sqlite", path=str(_resolve_path(source, store.path)))
    loop_raw = _optional_table(raw, "loop", source)
    _fields(loop_raw, _LOOP_FIELDS, "loop", source)
    loop_values: dict[str, Any] = {}
    for name in _LOOP_FIELDS:
        if name in loop_raw:
            loop_values[name] = loop_raw[name]
    loop_values.setdefault("system_prompt", "You are a helpful assistant.")
    try:
        loop = LoopConfig(**loop_values)
    except TypeError as exc:
        raise HarnessError(f"{source}: loop: {exc}") from exc
    _validate_loop(loop, source)

    tools_raw = raw.get("tools", [])
    if not isinstance(tools_raw, list) or not all(isinstance(item, dict) for item in tools_raw):
        _fail(source, "tools", "must be an array of tables")
    tools = tuple(_tool_config(item, index, source) for index, item in enumerate(tools_raw))
    skills_raw = _optional_table(raw, "skills", source)
    _fields(skills_raw, _SKILL_FIELDS, "skills", source)
    paths_raw = skills_raw.get("paths", [])
    if not isinstance(paths_raw, list) or not all(isinstance(item, str) for item in paths_raw):
        _fail(source, "skills.paths", "must be an array of paths")
    skills = SkillConfig(paths=tuple(str(_resolve_path(source, item)) for item in paths_raw))
    for skill_path in skills.paths:
        if not Path(skill_path).exists():
            _fail(source, "skills.paths", f"path does not exist: {skill_path}")
    trace_raw = _optional_table(raw, "trace", source)
    _fields(trace_raw, _TRACE_FIELDS, "trace", source)
    trace_exporter = trace_raw.get("exporter")
    if trace_exporter is not None:
        if not isinstance(trace_exporter, str) or not trace_exporter:
            _fail(source, "trace.exporter", "must be a nonempty string")
        if (
            trace_exporter != "stdout"
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*", trace_exporter)
            is None
        ):
            _fail(source, "trace.exporter", "must be 'stdout' or 'module.path:attribute'")
    trace = TraceConfig(exporter=trace_exporter)
    return HarnessSpec(
        schema=schema,
        min_nextloop=minimum,
        provider=provider,
        store=store,
        loop=loop,
        tools=tools,
        skills=skills,
        trace=trace,
        source_path=str(source),
    )


def _tool_config(raw: Mapping[str, Any], index: int, source: Path) -> ToolConfig:
    prefix = f"tools[{index}]"
    _fields(raw, _TOOL_FIELDS, prefix, source)
    driver = _choice(raw, "driver", ("python", "stdio", "mcp"), source, prefix)
    target = raw.get("target")
    command_raw = raw.get("command", [])
    if target is not None and not isinstance(target, str):
        _fail(source, f"{prefix}.target", "must be a string")
    if not isinstance(command_raw, list) or not all(
        isinstance(item, str) and item for item in command_raw
    ):
        _fail(source, f"{prefix}.command", "must be an array of nonempty strings")
    if driver == "python" and not target:
        _fail(source, f"{prefix}.target", "is required for the python driver")
    if driver in ("stdio", "mcp") and not command_raw:
        _fail(source, f"{prefix}.command", f"is required for the {driver} driver")
    if driver == "python" and command_raw:
        _fail(source, f"{prefix}.command", "is only valid for the stdio and mcp drivers")
    if driver in ("stdio", "mcp") and target:
        _fail(source, f"{prefix}.target", "is only valid for the python driver")
    command = tuple(
        str(_resolve_path(source, item)) if "/" in item or item.startswith(".") else item
        for item in command_raw
    )
    start = _choice(raw, "start", ("lazy", "eager"), source, prefix, default="lazy")
    resources = raw.get("resources", [])
    if not isinstance(resources, list) or not all(isinstance(item, str) for item in resources):
        _fail(source, f"{prefix}.resources", "must be an array of strings")
    timeout = raw.get("timeout_seconds")
    grace = _number(raw, "cancel_grace_seconds", 0.25, source, prefix)
    if timeout is not None and not is_positive_number(timeout):
        _fail(source, f"{prefix}.timeout_seconds", "must be a positive number")
    return ToolConfig(
        driver=driver,
        target=target,
        command=command,
        namespace=_optional_string(raw, "namespace", source, prefix),
        start=start,
        resources=tuple(resources),
        timeout_seconds=float(timeout) if timeout is not None else None,
        cancel_grace_seconds=grace,
    )


def _make_store(config: StoreConfig) -> InMemoryEventStore | SQLiteEventStore:
    if config.driver == "memory":
        return InMemoryEventStore()
    return SQLiteEventStore(config.path)


def _resolve_exporters(
    spec_exporter: str | None,
    runtime_exporter: TraceExporterInput | Sequence[TraceExporterInput] | None,
) -> tuple[TraceExporterInput, ...]:
    exporters: list[TraceExporterInput] = []
    if spec_exporter == "stdout":
        exporters.append(StdoutExporter())
    elif spec_exporter is not None:
        module_name, _, attribute = spec_exporter.partition(":")
        try:
            module = importlib.import_module(module_name)
            target = getattr(module, attribute)
        except (ImportError, AttributeError) as exc:
            raise HarnessError(f"Cannot load trace exporter '{spec_exporter}': {exc}") from exc
        exporters.append(target)
    if runtime_exporter is not None:
        if isinstance(runtime_exporter, Sequence) and not isinstance(
            runtime_exporter, (str, bytes)
        ):
            exporters.extend(runtime_exporter)
        else:
            exporters.append(runtime_exporter)
    for exporter in exporters:
        if not callable(exporter) and not callable(getattr(exporter, "export", None)):
            raise HarnessError(f"Trace exporter '{exporter}' is not callable and has no export()")
    return tuple(exporters)


def _fields(raw: Mapping[str, Any], allowed: set[str], prefix: str, source: Path) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        _fail(source, f"{prefix + '.' if prefix else ''}{unknown[0]}", "unknown field")


def _reject_secrets(raw: Mapping[str, Any], source: Path) -> None:
    forbidden = {
        "apikey",
        "secret",
        "secretkey",
        "token",
        "accesstoken",
        "refreshtoken",
        "sessiontoken",
        "clientsecret",
        "authorization",
        "proxyauthorization",
        "password",
        "credential",
        "credentials",
        "privatekey",
    }

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in forbidden:
                    _fail(
                        source, child_path, "plaintext credentials are forbidden; use api_key_env"
                    )
                visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(raw, "")


def _validate_loop(config: LoopConfig, source: Path) -> None:
    for name in ("system_prompt", "compaction_prompt"):
        if not isinstance(getattr(config, name), str):
            _fail(source, f"loop.{name}", "must be a string")
    for name in (
        "context_window",
        "output_reserve",
        "max_parallel_tools",
        "max_provider_attempts",
        "pacing_step_interval",
        "tool_result_bytes",
        "tool_result_full_max_bytes",
    ):
        value = getattr(config, name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            _fail(source, f"loop.{name}", "must be a positive integer")
    for name in ("max_protocol_corrections",):
        value = getattr(config, name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _fail(source, f"loop.{name}", "must be a nonnegative integer")
    for name in (
        "max_steps",
        "max_model_calls",
        "max_tool_calls",
        "max_input_tokens",
        "max_output_tokens",
        "max_total_tokens",
    ):
        value = getattr(config, name)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            _fail(source, f"loop.{name}", "must be null or a positive integer")
    deadline = config.turn_deadline_seconds
    if deadline is not None and (
        not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or deadline <= 0
    ):
        _fail(source, "loop.turn_deadline_seconds", "must be null or a positive number")
    if config.tool_result_full_max_bytes < config.tool_result_bytes:
        _fail(source, "loop.tool_result_full_max_bytes", "must be >= loop.tool_result_bytes")
    backoff = config.retry_backoff_seconds
    if not isinstance(backoff, (int, float)) or isinstance(backoff, bool) or backoff < 0:
        _fail(source, "loop.retry_backoff_seconds", "must be a nonnegative number")


def _version_tuple(value: str, source: Path, field: str) -> tuple[int, int, int, int, int]:
    try:
        return parse_version(value)
    except ValueError:
        _fail(source, field, f"invalid version '{value}'")


def _resolve_path(source: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (source.parent / path).resolve()


def _table(raw: Mapping[str, Any], name: str, source: Path) -> Mapping[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        _fail(source, name, "must be a table")
    return cast(Mapping[str, Any], value)


def _optional_table(
    raw: Mapping[str, Any], name: str, source: Path, prefix: str = ""
) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        _fail(source, f"{prefix + '.' if prefix else ''}{name}", "must be a table")
    return cast(Mapping[str, Any], value)


def _optional_string(raw: Mapping[str, Any], name: str, source: Path, prefix: str) -> str:
    value = raw.get(name, "")
    if not isinstance(value, str):
        _fail(source, f"{prefix}.{name}", "must be a string")
    return value


def _string(
    raw: Mapping[str, Any],
    name: str,
    source: Path,
    prefix: str = "",
    default: str | None = None,
) -> str:
    value = raw.get(name, default)
    if not isinstance(value, str) or not value:
        _fail(source, f"{prefix + '.' if prefix else ''}{name}", "must be a nonempty string")
    return value


def _choice(
    raw: Mapping[str, Any],
    name: str,
    choices: Sequence[str],
    source: Path,
    prefix: str = "",
    default: str | None = None,
) -> str:
    value = _string(raw, name, source, prefix, default)
    if value not in choices:
        _fail(
            source,
            f"{prefix + '.' if prefix else ''}{name}",
            f"must be one of {', '.join(choices)}",
        )
    return value


def _number(
    raw: Mapping[str, Any], name: str, default: float, source: Path, prefix: str = ""
) -> float:
    value = raw.get(name, default)
    if not is_positive_number(value):
        _fail(source, f"{prefix + '.' if prefix else ''}{name}", "must be a positive number")
    return float(value)


def _fail(source: Path, field: str, reason: str) -> NoReturn:
    raise HarnessError(f"{source}: {field}: {reason}")
