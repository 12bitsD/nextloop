from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts import (
    Cancelled,
    Completed,
    HarnessError,
    ToolCall,
    ToolConfig,
    ToolOutcome,
    ToolSpec,
    UncertainHanging,
)
from .cleanup import wait_for_cleanup

MAX_FRAME_BYTES = 16 * 1024 * 1024


class StdioProtocolError(HarnessError):
    pass


async def read_frame(reader: asyncio.StreamReader, buffer: bytearray) -> bytes:
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


class StdioTransport:
    def __init__(self, config: ToolConfig, *, sensitive_env: Sequence[str] = ()) -> None:
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
        self._broken = False
        self._specs: tuple[ToolSpec, ...] = ()
        self._remote_names: dict[str, str] = {}

    @property
    def started(self) -> bool:
        return self._process is not None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    async def _spawn(self) -> None:
        if self._closed:
            raise StdioProtocolError("stdio worker is closed")
        if self._broken:
            raise StdioProtocolError("stdio worker is unavailable after a crash")
        if self._process is not None:
            return
        self._process = await asyncio.create_subprocess_exec(
            *self.config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            env=self._environment,
        )
        self._reader = asyncio.create_task(self._read_messages())

    async def start(self) -> None:
        raise NotImplementedError

    def _call_params(
        self,
        remote_name: str,
        arguments: Mapping[str, Any],
        session_id: str,
        turn: int,
        step: int,
    ) -> Mapping[str, Any]:
        raise NotImplementedError

    def _response_outcome(self, response: Mapping[str, Any], call: ToolCall) -> ToolOutcome:
        raise NotImplementedError

    async def _cancel_request(self, request_id: str, remote_name: str) -> None:
        raise NotImplementedError

    def _unknown_tool(self, call: ToolCall) -> ToolOutcome:
        return Completed(f"Unknown stdio Tool: {call.name}", is_error=True)

    async def _execute_with_cancel(
        self,
        call: ToolCall,
        arguments: Mapping[str, Any],
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
            return self._unknown_tool(call)
        if self._broken or self._process is None or self._process.returncode is not None:
            return self._hanging(call.name, spec.resources, "stdio worker is not running")
        request_id = self._reserve_request_id()
        operation = asyncio.create_task(
            self._request_message(
                "tools/call",
                self._call_params(remote_name, arguments, session_id, turn, step),
                request_id=request_id,
            )
        )
        cancellation = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {operation, cancellation},
                timeout=spec.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                return self._response_outcome(await operation, call)
            timed_out = cancellation not in done
            await self._cleanup_call(operation, request_id, remote_name)
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
            await self._cleanup_call(operation, request_id, remote_name)
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
            await self._cleanup_call(operation, request_id, remote_name)
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

    async def _cleanup_call(
        self, operation: asyncio.Task[dict[str, Any]], request_id: str, remote_name: str
    ) -> None:
        cleanup = asyncio.create_task(self._abort_call(operation, request_id, remote_name))
        await wait_for_cleanup(cleanup)

    async def _abort_call(
        self, operation: asyncio.Task[dict[str, Any]], request_id: str, remote_name: str
    ) -> None:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        try:
            with contextlib.suppress(ConnectionError, StdioProtocolError, TimeoutError):
                await self._cancel_request(request_id, remote_name)
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

    @staticmethod
    def _hanging(name: str, resources: tuple[str, ...], reason: str) -> UncertainHanging:
        return UncertainHanging(
            f"Tool {name}: {reason}; external effects are unknown", resource_locks=resources
        )

    async def _request(
        self, method: str, params: Mapping[str, Any], request_timeout: float | None = None
    ) -> dict[str, Any]:
        response = await self._request_message(method, params, request_timeout)
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

    def _reserve_request_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    async def _request_message(
        self,
        method: str,
        params: Mapping[str, Any],
        request_timeout: float | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise ConnectionError("stdio worker is not running")
        if request_id is None:
            request_id = self._reserve_request_id()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
            )
            if request_timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=request_timeout)
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
                line = await read_frame(process.stdout, buffer)
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
