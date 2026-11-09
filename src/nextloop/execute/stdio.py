from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Mapping, Sequence
from typing import Any

import jsonschema

from ..contracts import (
    STDIO_PROTOCOL_VERSION,
    Cancelled,
    Completed,
    HarnessError,
    ToolCall,
    ToolConfig,
    ToolExecutionMode,
    ToolOutcome,
    ToolSpec,
    UncertainHanging,
    ValidatedCall,
    ValidationIssue,
    is_positive_number,
)
from .cleanup import wait_for_cleanup
from .tools import validate_calls

MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_INLINE_CONTENT_BYTES = 1024 * 1024


class StdioProtocolError(HarnessError):
    pass


async def _read_frame(reader: asyncio.StreamReader, buffer: bytearray) -> bytes:
    while True:
        newline = buffer.find(b"\n")
        if newline >= 0:
            if newline > MAX_FRAME_BYTES:
                raise StdioProtocolError("stdio frame exceeds 16MiB limit")
            frame = bytes(buffer[:newline])
            del buffer[: newline + 1]
            return frame
        chunk = await reader.read(64 * 1024)
        if not chunk:
            if len(buffer) > MAX_FRAME_BYTES:
                raise StdioProtocolError("stdio frame exceeds 16MiB limit")
            frame = bytes(buffer)
            buffer.clear()
            return frame
        buffer.extend(chunk)
        if len(buffer) > MAX_FRAME_BYTES and b"\n" not in buffer:
            raise StdioProtocolError("stdio frame exceeds 16MiB limit")


class StdioDriver:
    def __init__(self, config: ToolConfig, *, sensitive_env: Sequence[str] = ()) -> None:
        if config.driver != "stdio" or not config.command:
            raise ValueError("StdioDriver requires a stdio ToolConfig with a command")
        self.config = config
        self._environment = {
            key: value for key, value in os.environ.items() if key not in set(sensitive_env)
        }
        self._closed = False
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._next_id = 0
        self._specs: tuple[ToolSpec, ...] = ()
        self._remote_names: dict[str, str] = {}
        self._broken = False

    @property
    def started(self) -> bool:
        return self._process is not None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    async def start(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise StdioProtocolError("stdio worker is closed")
            if self._specs:
                return
            if self._broken:
                raise StdioProtocolError("stdio worker is unavailable after a crash")
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self.config.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=None,
                    env=self._environment,
                )
                self._reader = asyncio.create_task(self._read_messages())
                response = await self._request(
                    "initialize",
                    {
                        "protocolVersion": STDIO_PROTOCOL_VERSION,
                        "clientInfo": {"name": "nextloop", "version": "0.3.0rc2"},
                    },
                    request_timeout=10.0,
                )
                if response.get("protocolVersion") != STDIO_PROTOCOL_VERSION:
                    raise StdioProtocolError(
                        "stdio worker returned an unsupported protocol version"
                    )
                listed = await self._request("tools/list", {}, request_timeout=10.0)
                self._specs = self._parse_specs(listed.get("tools"))
            except BaseException:
                await wait_for_cleanup(asyncio.create_task(self._terminate()))
                raise

    def catalog(self) -> Sequence[ToolSpec]:
        return self._specs

    def validate(
        self, calls: Sequence[ToolCall]
    ) -> tuple[tuple[ValidatedCall, ...], tuple[ValidationIssue, ...]]:
        return validate_calls(calls, {spec.name: spec for spec in self._specs})

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
        if cancel.is_set():
            return Cancelled(settled=True, partial_content="Cancelled before stdio Tool call")
        if not await self._start_for_call(cancel):
            return Cancelled(
                settled=False,
                partial_content=f"stdio Tool {call.name} was cancelled during lazy startup",
                resource_locks=self.config.resources,
            )
        remote_name = self._remote_names.get(call.name)
        spec = next((item for item in self._specs if item.name == call.name), None)
        if remote_name is None or spec is None:
            return Completed(f"Unknown stdio Tool: {call.name}", is_error=True)
        if self._broken or self._process is None or self._process.returncode is not None:
            return self._hanging(call.name, spec.resources, "stdio worker is not running")
        operation = asyncio.create_task(
            self._request(
                "tools/call",
                {
                    "name": remote_name,
                    "arguments": dict(arguments),
                    "context": {"session_id": session_id, "turn": turn, "step": step},
                },
            )
        )
        cancellation = asyncio.create_task(cancel.wait())
        timeout = spec.timeout_seconds
        try:
            done, _ = await asyncio.wait(
                {operation, cancellation},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                response = await operation
                return self._outcome(response)
            timed_out = cancellation not in done
            await self._cleanup_call(operation, call.name, remote_name)
            if timed_out:
                return self._hanging(call.name, spec.resources, "stdio Tool call timed out")
            return Cancelled(
                settled=False,
                partial_content=f"stdio Tool {call.name} was cancelled; effects are unknown",
                resource_locks=spec.resources,
            )
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            await self._cleanup_call(operation, call.name, remote_name)
            return Cancelled(
                settled=False,
                partial_content=f"stdio Tool {call.name} task was cancelled; effects are unknown",
                resource_locks=spec.resources,
            )
        except (
            StdioProtocolError,
            ConnectionError,
            asyncio.IncompleteReadError,
            ValueError,
        ) as exc:
            await self._cleanup_call(operation, call.name, remote_name)
            return self._hanging(call.name, spec.resources, str(exc))
        finally:
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)

    async def _start_for_call(self, cancel: asyncio.Event) -> bool:
        starting = asyncio.create_task(self.start())
        cancellation = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {starting, cancellation}, return_when=asyncio.FIRST_COMPLETED
            )
            if starting in done:
                await starting
                return True
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
            return False
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            starting.cancel()
            waiting = asyncio.create_task(self._await_start_cleanup(starting))
            await wait_for_cleanup(waiting)
            return False
        finally:
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)

    @staticmethod
    async def _await_start_cleanup(starting: asyncio.Task[None]) -> None:
        await asyncio.gather(starting, return_exceptions=True)

    async def close(self) -> None:
        cancelled = await wait_for_cleanup(asyncio.create_task(self._close()))
        if cancelled:
            raise asyncio.CancelledError()

    async def _close(self) -> None:
        async with self._start_lock:
            self._closed = True
            process = self._process
            try:
                if process is not None and process.returncode is None and not self._broken:
                    try:
                        await self._request("shutdown", {}, request_timeout=2.0)
                        await asyncio.wait_for(process.wait(), timeout=1.0)
                    except (Exception, TimeoutError):
                        pass
            finally:
                await self._terminate()

    async def _cleanup_call(
        self, operation: asyncio.Task[dict[str, Any]], name: str, remote_name: str
    ) -> None:
        cleanup = asyncio.create_task(self._abort_call(operation, name, remote_name))
        await wait_for_cleanup(cleanup)

    async def _abort_call(
        self, operation: asyncio.Task[dict[str, Any]], name: str, remote_name: str
    ) -> None:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        try:
            await self._cancel_request(name, remote_name)
            await asyncio.sleep(self.config.cancel_grace_seconds)
        finally:
            process = self._process
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.config.cancel_grace_seconds)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            self._broken = True
            if self._reader is not None:
                await asyncio.gather(self._reader, return_exceptions=True)

    async def _cancel_request(self, name: str, remote_name: str) -> None:
        with contextlib.suppress(ConnectionError, StdioProtocolError, TimeoutError):
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"name": remote_name, "reason": f"nextloop cancelled {name}"},
                },
                send_timeout=0.05,
            )

    async def _request(
        self, method: str, params: Mapping[str, Any], request_timeout: float | None = None
    ) -> dict[str, Any]:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise ConnectionError("stdio worker is not running")
        self._next_id += 1
        request_id = str(self._next_id)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
            )
            if request_timeout is None:
                response = await future
            else:
                response = await asyncio.wait_for(future, timeout=request_timeout)
            if "error" in response:
                error = response["error"]
                message = (
                    error.get("message", "unknown JSON-RPC error")
                    if isinstance(error, dict)
                    else str(error)
                )
                raise StdioProtocolError(f"{method}: {message}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise StdioProtocolError(f"{method}: result must be an object")
            return result
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _send(self, message: Mapping[str, Any], send_timeout: float = 1.0) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise ConnectionError("stdio worker stdin is closed")
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(data) - 1 > MAX_FRAME_BYTES:
            raise StdioProtocolError("stdio request frame exceeds 16MiB limit")
        async with self._write_lock:
            try:
                process.stdin.write(data)
                await asyncio.wait_for(process.stdin.drain(), timeout=send_timeout)
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._broken = True
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.1)
                except TimeoutError:
                    raise ConnectionError("stdio worker stdin is closed") from exc
                raise ConnectionError(
                    f"stdio worker exited with status {process.returncode}"
                ) from exc
            except TimeoutError as exc:
                self._broken = True
                raise ConnectionError("stdio worker stdin is backpressured") from exc

    async def _read_messages(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        buffer = bytearray()
        try:
            while True:
                line = await _read_frame(process.stdout, buffer)
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise StdioProtocolError(
                        "stdio worker wrote non-JSON output to stdout"
                    ) from exc
                if not isinstance(message, dict) or "id" not in message:
                    continue
                future = self._pending.get(str(message["id"]))
                if future is not None and not future.done():
                    future.set_result(message)
        except Exception as exc:
            self._broken = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(exc if isinstance(exc, Exception) else ConnectionError())
        finally:
            if process.returncode is None:
                await process.wait()
            if process.returncode != 0:
                self._broken = True
            error = ConnectionError(f"stdio worker exited with status {process.returncode}")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)

    async def _terminate(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            try:
                process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(process.wait(), timeout=0.25)
            except (ProcessLookupError, TimeoutError):
                if process.returncode is None:
                    process.kill()
                    await process.wait()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)
        self._process = None
        self._reader = None

    def _parse_specs(self, raw: Any) -> tuple[ToolSpec, ...]:
        worker = " ".join(self.config.command)
        if not isinstance(raw, list):
            raise StdioProtocolError(
                f"stdio worker {worker}: tools/list result.tools must be an array"
            )
        specs = []
        for item in raw:
            try:
                if not isinstance(item, dict):
                    raise TypeError("Tool specification must be an object")
                remote_name = item["name"]
                description = item.get("description", "")
                parameters = item.get("parameters", {"type": "object"})
                resources = item.get("resources", list(self.config.resources))
                if not isinstance(remote_name, str) or not remote_name:
                    raise TypeError("name must be a nonempty string")
                if not isinstance(description, str):
                    raise TypeError("description must be a string")
                if not isinstance(parameters, dict):
                    raise TypeError("parameters must be an object")
                if not isinstance(resources, list) or not all(
                    isinstance(resource, str) for resource in resources
                ):
                    raise TypeError("resources must be an array of strings")
                jsonschema.Draft202012Validator.check_schema(parameters)
                remote_timeout = item.get("timeout_seconds")
                if remote_timeout is not None and not is_positive_number(remote_timeout):
                    raise TypeError("timeout_seconds must be a positive number")
                timeout = self.config.timeout_seconds or remote_timeout
                name = (
                    f"{self.config.namespace}__{remote_name}"
                    if self.config.namespace
                    else remote_name
                )
                specs.append(
                    ToolSpec(
                        name=name,
                        description=description,
                        parameters=parameters,
                        mode=ToolExecutionMode(item.get("mode", "exclusive")),
                        resources=tuple(resources),
                        timeout_seconds=float(timeout) if timeout is not None else None,
                    )
                )
                self._remote_names[name] = remote_name
            except (KeyError, TypeError, ValueError, jsonschema.SchemaError) as exc:
                tool_name = item.get("name", "<unknown>") if isinstance(item, dict) else "<unknown>"
                raise StdioProtocolError(
                    f"stdio worker {worker}: invalid Tool '{tool_name}': {exc}"
                ) from exc
        return tuple(specs)

    @staticmethod
    def _outcome(response: Mapping[str, Any]) -> ToolOutcome:
        allowed = {
            "content",
            "structured",
            "assets",
            "is_error",
            "concludes_turn",
            "release_resources",
        }
        unknown = set(response) - allowed
        if unknown:
            raise StdioProtocolError(f"Unknown Tool result field: {sorted(unknown)[0]}")
        content = response.get("content", "")
        if not isinstance(content, str):
            raise StdioProtocolError("Tool result content must be a string")
        if len(content.encode()) > MAX_INLINE_CONTENT_BYTES:
            raise StdioProtocolError(
                "Inline Tool content exceeds 1MiB; return an asset reference instead"
            )
        for field in ("is_error", "concludes_turn"):
            if not isinstance(response.get(field, False), bool):
                raise StdioProtocolError(f"Tool result {field} must be boolean")
        references: dict[str, tuple[str, ...]] = {}
        for field in ("assets", "release_resources"):
            value = response.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise StdioProtocolError(f"Tool result {field} must be an array of strings")
            references[field] = tuple(value)
        return Completed(
            content,
            is_error=response.get("is_error", False),
            concludes_turn=response.get("concludes_turn", False),
            structured=response.get("structured"),
            assets=references["assets"],
            release_resources=references["release_resources"],
        )

    @staticmethod
    def _hanging(name: str, resources: tuple[str, ...], reason: str) -> UncertainHanging:
        return UncertainHanging(
            f"Tool {name}: {reason}; external effects are unknown", resource_locks=resources
        )
