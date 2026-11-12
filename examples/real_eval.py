from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from nextloop import (
    AgentLoop,
    InMemoryEventStore,
    LoopConfig,
    OpenAIProvider,
    ToolRegistry,
    ToolResultMode,
    ToolSpec,
    TurnContext,
    with_result_reads,
)
from nextloop.events.project import message_to_data

SHARED_CONTEXT = (
    "Agent runtime context: event logs are append-only. Review pending work, "
    "preserve tool results, and finish with a short answer. " * 500
)


class RecordingProvider(OpenAIProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.case_id = "unassigned"
        self.records = []
        self.fail_once_cases = set()
        self._request_hashes = []

    async def complete(self, request, *, attempt, cancel):
        return await self._record_completion(request, attempt=attempt, cancel=cancel)

    async def complete_with_preview(self, request, *, attempt, cancel, emit):
        return await self._record_completion(request, attempt=attempt, cancel=cancel, emit=emit)

    async def _record_completion(self, request, *, attempt, cancel, emit=None):
        message_hashes = [
            hashlib.sha256(
                json.dumps(message_to_data(message), ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            for message in request.messages
        ]
        shared_prefix = max(
            (
                next(
                    (
                        index
                        for index, pair in enumerate(zip(message_hashes, previous, strict=False))
                        if pair[0] != pair[1]
                    ),
                    min(len(message_hashes), len(previous)),
                )
                for previous in self._request_hashes
            ),
            default=0,
        )
        self._request_hashes.append(message_hashes)
        started = time.monotonic()
        first_output = None
        record = {
            "case_id": self.case_id,
            "turn": request.turn,
            "step": request.step,
            "attempt": attempt,
            "message_count": len(message_hashes),
            "shared_prefix_messages": shared_prefix,
            "request_sha256": hashlib.sha256("".join(message_hashes).encode()).hexdigest(),
            "started_monotonic": started,
        }

        def record_output(delta):
            nonlocal first_output
            if first_output is None and delta.get("channel") in ("content", "tool_call"):
                first_output = time.monotonic()
            if emit is not None:
                emit(delta)

        try:
            if self.case_id in self.fail_once_cases:
                self.fail_once_cases.remove(self.case_id)
                from nextloop import ProviderError

                raise ProviderError("Injected transient provider failure", retryable=True)
            response = await self._complete(
                request, attempt=attempt, cancel=cancel, emit=record_output
            )
        except Exception as exc:
            ended = time.monotonic()
            self.records.append(
                record
                | {
                    "usage": None,
                    "finish_reason": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "ttft_seconds": None,
                    "streaming_seconds": None,
                    "elapsed_seconds": round(ended - started, 6),
                    "ended_monotonic": ended,
                }
            )
            raise
        ended = time.monotonic()
        ttft = first_output - started if first_output is not None else None
        self.records.append(
            record
            | {
                "usage": response.usage.__dict__ if response.usage else None,
                "finish_reason": response.finish_reason.value,
                "tool_call_count": len(response.tool_calls),
                "ttft_seconds": round(ttft, 6) if ttft is not None else None,
                "streaming_seconds": round(ended - first_output, 6)
                if first_output is not None
                else None,
                "elapsed_seconds": round(ended - started, 6),
                "ended_monotonic": ended,
            }
        )
        return response


class RecordingDelivery:
    def __init__(self) -> None:
        self.messages = []

    async def deliver(self, message):
        self.messages.append(message)


CASES = [
    ("basic-read", ["Read README.md using read_file.", "Summarize the Tool result."]),
    (
        "read-two",
        [
            "Use read_file to read a.txt.",
            "Use read_file to read b.txt and compare both results.",
        ],
    ),
    (
        "write-read",
        [
            "Use write_file to save 'release-note' to notes.txt.",
            "Use read_file to read notes.txt and summarize it.",
        ],
    ),
    (
        "search-read",
        [
            "Use search_files to find files matching 'next'.",
            "Use read_file to read a.txt and summarize it.",
        ],
    ),
    (
        "math",
        [
            "Use calculate to evaluate '2+2'.",
            "Use calculate to evaluate '40+2' and explain the result.",
        ],
    ),
    (
        "weather-time",
        [
            "Use get_weather to read the weather in Shanghai.",
            "Use get_time to read the current UTC time.",
        ],
    ),
    (
        "list-read",
        [
            "Use list_files to list the current directory.",
            "Use read_file to read a.txt and summarize it.",
        ],
    ),
    (
        "large-result",
        [
            "Use read_file to read big.txt.",
            "Use read_tool_result with the event_id from the previous Tool result "
            "to read offset 256 and limit 512, then summarize it.",
        ],
    ),
    (
        "long-workflow",
        [
            "Use read_file to read a.txt.",
            "Use read_file to read b.txt.",
            "Use calculate to evaluate '40+2'.",
            "Summarize all previous Tool results.",
        ],
    ),
    (
        "mixed-tools",
        [
            "Use get_time to read the current UTC time.",
            "Use get_weather to read the weather in Shanghai.",
            "Use read_file to read a.txt and summarize everything.",
        ],
    ),
]


def build_tools(invocations):
    tools = ToolRegistry()

    def record(name, result):
        def handler(arguments, context):
            started = time.monotonic()
            invocation = {
                "name": name,
                "arguments": dict(arguments),
                "turn": context.turn,
                "step": context.step,
                "started_monotonic": started,
            }
            invocations.append(invocation)
            try:
                return result(arguments)
            finally:
                invocation["ended_monotonic"] = time.monotonic()

        return handler

    tools.register(
        ToolSpec(
            "read_file",
            "Read a file",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            result_mode=ToolResultMode.PREVIEW,
        ),
        record(
            "read_file",
            lambda arguments: (
                "x" * 20_000
                if arguments["path"] == "big.txt"
                else f"Contents of {arguments['path']}: hello from nextloop"
            ),
        ),
    )
    tools.register(
        ToolSpec(
            "write_file",
            "Write a file",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        ),
        record(
            "write_file", lambda arguments: f"Wrote {arguments['path']}: {arguments['content']}"
        ),
    )
    tools.register(
        ToolSpec(
            "search_files",
            "Search files",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        record("search_files", lambda arguments: ["a.txt", "b.txt", "nextloop.md"]),
    )
    tools.register(
        ToolSpec(
            "calculate",
            "Calculate an arithmetic expression",
            {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
                "additionalProperties": False,
            },
        ),
        record("calculate", lambda arguments: "42" if arguments["expression"] == "40+2" else "4"),
    )
    tools.register(
        ToolSpec(
            "get_weather",
            "Get weather",
            {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        ),
        record("get_weather", lambda arguments: f"Weather in {arguments['city']}: sunny, 22C"),
    )
    tools.register(
        ToolSpec("get_time", "Get current time", {"type": "object", "additionalProperties": False}),
        record("get_time", lambda arguments: "2026-11-13T00:00:00Z"),
    )
    tools.register(
        ToolSpec(
            "list_files",
            "List files",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        record("list_files", lambda arguments: ["a.txt", "b.txt", "big.txt"]),
    )
    return tools


def occupied_seconds(intervals):
    ordered = sorted(intervals)
    if not ordered:
        return 0.0
    total = 0.0
    start, end = ordered[0]
    for next_start, next_end in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += end - start
            start, end = next_start, next_end
    return total + end - start


def measure_turn(provider, invocations, case_id, turn, started, ended):
    model_intervals = [
        (record["started_monotonic"], record["ended_monotonic"])
        for record in provider.records
        if record["case_id"] == case_id and record["turn"] == turn
    ]
    tool_intervals = [
        (invocation["started_monotonic"], invocation["ended_monotonic"])
        for invocation in invocations
        if invocation.get("turn") == turn and "ended_monotonic" in invocation
    ]
    wall = ended - started
    model = occupied_seconds(model_intervals)
    tool = occupied_seconds(tool_intervals)
    occupied = occupied_seconds([*model_intervals, *tool_intervals])
    return {
        "turn": turn,
        "wall_seconds": round(wall, 6),
        "model_seconds": round(model, 6),
        "tool_seconds": round(tool, 6),
        "framework_seconds": round(max(0.0, wall - occupied), 6),
        "framework_ratio": round(max(0.0, wall - occupied) / wall, 6) if wall else 0,
    }


def timing_summary(cases, provider_records):
    turns = [timing for case in cases for timing in case.get("turn_timings", ())]
    ttft = sorted(
        record["ttft_seconds"]
        for record in provider_records
        if record.get("ttft_seconds") is not None
    )
    wall = sum(turn["wall_seconds"] for turn in turns)
    framework = sum(turn["framework_seconds"] for turn in turns)
    return {
        "measured_turns": len(turns),
        "turn_wall_seconds": round(wall, 6),
        "model_seconds": round(sum(turn["model_seconds"] for turn in turns), 6),
        "tool_seconds": round(sum(turn["tool_seconds"] for turn in turns), 6),
        "framework_seconds": round(framework, 6),
        "framework_ratio": round(framework / wall, 6) if wall else 0,
        "ttft_samples": len(ttft),
        "median_ttft_seconds": ttft[len(ttft) // 2] if ttft else None,
        "p95_ttft_seconds": ttft[max(0, int(len(ttft) * 0.95) - 1)] if ttft else None,
    }


async def evaluate_case(provider, case_id, prompts, *, config=None, store=None):
    provider.case_id = case_id
    invocations = []
    delivery = RecordingDelivery()
    store = store or InMemoryEventStore()
    tools = with_result_reads(build_tools(invocations), store, page_bytes=512)
    loop = AgentLoop()
    outcomes = []
    turn_timings = []
    try:
        for prompt in prompts:
            started = time.monotonic()
            outcome = await loop.run(
                TurnContext(
                    session_id=case_id,
                    initial_input=prompt,
                    config=config
                    or LoopConfig(
                        system_prompt=SHARED_CONTEXT,
                        max_provider_attempts=1,
                        retry_backoff_seconds=0,
                        max_steps=6,
                        tool_result_bytes=512,
                        tool_result_full_max_bytes=2048,
                    ),
                    store=store,
                    provider=provider,
                    tools=tools,
                    delivery=delivery,
                )
            )
            turn_timings.append(
                measure_turn(
                    provider, invocations, case_id, outcome.turn, started, time.monotonic()
                )
            )
            outcomes.append(
                {
                    "status": outcome.status.value,
                    "turn": outcome.turn,
                    "step": outcome.step,
                    "final_message_id": outcome.final_message_id,
                    "error": outcome.error,
                }
            )
    except Exception as exc:
        outcomes.append({"error": f"{type(exc).__name__}: {exc}"})
    return {
        "case_id": case_id,
        "outcomes": outcomes,
        "turn_timings": turn_timings,
        "tool_invocations": invocations,
        "delivered": [message.content for message in delivery.messages],
    }


async def main():
    import os

    provider = RecordingProvider(
        model_id="kimi-k2.8-highspeed",
        base_url="https://free-tokens.msh.team/v1",
        api_key=os.environ["KIMI_API_KEY"],
        default_parameters={"max_tokens": 192},
        timeout_seconds=60,
    )
    cases = []
    for case_id, prompts in CASES:
        result = await evaluate_case(provider, case_id, prompts)
        cases.append(result)
        print(
            json.dumps(
                {
                    "case_id": case_id,
                    "outcomes": result["outcomes"],
                    "tool_calls": len(result["tool_invocations"]),
                },
                ensure_ascii=False,
            )
        )
    await provider.close()
    usage_records = provider.records
    total_input = sum(record["usage"]["input_tokens"] or 0 for record in usage_records)
    total_cached = sum(record["usage"]["cached_input_tokens"] or 0 for record in usage_records)
    total_output = sum(record["usage"]["output_tokens"] or 0 for record in usage_records)
    total_elapsed = sum(record["elapsed_seconds"] for record in usage_records)
    summary = {
        "model": "kimi-k2.8-highspeed",
        "case_count": len(cases),
        "model_call_count": len(usage_records),
        "tool_call_count": sum(len(case["tool_invocations"]) for case in cases),
        "input_tokens": total_input,
        "cached_input_tokens": total_cached,
        "cache_ratio": round(total_cached / total_input, 4) if total_input else 0,
        "output_tokens": total_output,
        "total_elapsed_seconds": round(total_elapsed, 3),
        "cached_call_seconds": round(
            sum(
                record["elapsed_seconds"]
                for record in usage_records
                if record["usage"]["cached_input_tokens"]
            ),
            3,
        ),
        "uncached_call_seconds": round(
            sum(
                record["elapsed_seconds"]
                for record in usage_records
                if not record["usage"]["cached_input_tokens"]
            ),
            3,
        ),
        "timing": timing_summary(cases, usage_records),
    }
    output = {"summary": summary, "provider_calls": usage_records, "cases": cases}
    destination = Path("docs/research/real-eval-2026-11-13/results.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        destination.write_text, json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps({"summary": summary, "results": str(destination)}, ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    asyncio.run(main())
