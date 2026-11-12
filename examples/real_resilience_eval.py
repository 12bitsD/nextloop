from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path

from real_eval import (
    SHARED_CONTEXT,
    RecordingDelivery,
    RecordingProvider,
    build_tools,
    evaluate_case,
    timing_summary,
)

from nextloop import (
    AgentLoop,
    ApprovalPolicy,
    Cancelled,
    Completed,
    EventType,
    InMemoryEventStore,
    LoopConfig,
    ModelCallTrace,
    ToolEffect,
    ToolRegistry,
    ToolSpec,
    TurnContext,
    project_traces,
    with_result_reads,
)

LONG_CASES = [
    (
        "long-single-turn",
        [
            "Use read_file once for each of these eight files: alpha.txt, beta.txt, "
            "gamma.txt, delta.txt, epsilon.txt, zeta.txt, eta.txt, theta.txt. "
            "You may call independent reads in parallel. After all eight results arrive, "
            "give a detailed consolidated report of at least 200 words."
        ],
        8,
    ),
    (
        "long-multi-turn",
        [
            *[
                f"Use read_file to read {name}.txt, then describe its actual result."
                for name in (
                    "alpha",
                    "beta",
                    "gamma",
                    "delta",
                    "epsilon",
                    "zeta",
                    "eta",
                    "theta",
                )
            ],
            "Review all eight earlier Tool results and write a consolidated report of "
            "at least 250 words. Do not call more tools.",
        ],
        8,
    ),
    (
        "iterative-calculations",
        [
            "Use calculate to evaluate '2+2', then report the result.",
            "Use calculate to evaluate '40+2', then report the result.",
            "Use calculate to evaluate '2+2' again, then compare it with the first result.",
            "Use calculate to evaluate '40+2' again, then compare all four Tool results.",
            "Without more tools, explain the calculation history in at least 150 words.",
        ],
        4,
    ),
    (
        "long-context-reasoning",
        [
            "Use read_file to read alpha.txt and beta.txt.",
            "Using the previous Tool results, write a structured analysis of at least 200 words.",
            "Without calling tools, extend the previous analysis with risks and mitigations.",
            "Without calling tools, produce a final detailed summary of the whole session.",
        ],
        2,
    ),
]


def config(**overrides):
    values = {
        "system_prompt": SHARED_CONTEXT,
        "max_provider_attempts": 2,
        "retry_backoff_seconds": 0,
        "max_steps": 24,
        "tool_result_bytes": 512,
        "tool_result_full_max_bytes": 2048,
    }
    values.update(overrides)
    return LoopConfig(**values)


def context(session_id, prompt, store, provider, tools, delivery, **overrides):
    return TurnContext(
        session_id=session_id,
        initial_input=prompt,
        config=config(**overrides),
        store=store,
        provider=provider,
        tools=tools,
        delivery=delivery,
    )


async def event_summary(store, session_id):
    events = await store.load_recovery(session_id)
    starts = {
        (event.turn, event.step) for event in events if event.event_type == EventType.STEP_START
    }
    ends = {(event.turn, event.step) for event in events if event.event_type == EventType.STEP_END}
    return {
        "event_count": len(events),
        "event_types": dict(Counter(event.event_type.value for event in events)),
        "model_ttft_ms": [
            trace.ttft_ms
            for trace in project_traces(events)
            if isinstance(trace, ModelCallTrace) and trace.ttft_ms is not None
        ],
        "unclosed_steps": sorted(list(starts - ends)),
        "unexpected_step_ends": sorted(list(ends - starts)),
    }


async def run_long_cases(provider):
    results = []
    for case_id, prompts, expected_tools in LONG_CASES:
        provider.case_id = case_id
        store = InMemoryEventStore()
        started = time.monotonic()
        result = await evaluate_case(provider, case_id, prompts, config=config(), store=store)
        result["events"] = await event_summary(store, case_id)
        result["wall_seconds"] = round(time.monotonic() - started, 3)
        result["passed"] = (
            all(outcome.get("status") == "succeeded" for outcome in result["outcomes"])
            and len(result["tool_invocations"]) >= expected_tools
            and not result["events"]["unclosed_steps"]
            and not result["events"]["unexpected_step_ends"]
        )
        result["expected_minimum_tools"] = expected_tools
        results.append(result)
    return results


async def run_paged_result(provider):
    case_id = "paged-result"
    provider.case_id = case_id
    store = InMemoryEventStore()
    first = await evaluate_case(
        provider, case_id, ["Use read_file to read big.txt."], config=config(), store=store
    )
    result_event = next(
        event
        for event in await store.load_recovery(case_id)
        if event.event_type == EventType.TOOL_RESULT
    )
    second = await evaluate_case(
        provider,
        case_id,
        [
            f"Call read_tool_result with event_id={result_event.event_id!r}, offset=256, "
            "limit=512. Report the page metadata from its actual result."
        ],
        config=config(),
        store=store,
    )
    calls = [
        event
        for event in await store.load_recovery(case_id)
        if event.event_type == EventType.TOOL_CALL
        and event.payload.get("name") == "read_tool_result"
    ]
    pages = [
        event
        for event in await store.load_recovery(case_id)
        if event.event_type == EventType.TOOL_RESULT
        and "Tool result page:" in str(event.payload.get("outcome", {}).get("content", ""))
    ]
    return {
        "case_id": case_id,
        "outcomes": first["outcomes"] + second["outcomes"],
        "turn_timings": first["turn_timings"] + second["turn_timings"],
        "tool_invocations": first["tool_invocations"] + second["tool_invocations"],
        "delivered": first["delivered"] + second["delivered"],
        "events": await event_summary(store, case_id),
        "paged_calls": len(calls),
        "valid_pages": len(pages),
        "passed": bool(calls and pages),
    }


async def run_compaction(provider):
    case_id = "compaction"
    provider.case_id = case_id
    store = InMemoryEventStore()
    delivery = RecordingDelivery()
    tools = with_result_reads(build_tools([]), store)
    outcome = await AgentLoop().run(
        context(
            case_id,
            "Review this long task background, then summarize the current objective: " + "x" * 6000,
            store,
            provider,
            tools,
            delivery,
            system_prompt="Review the task and provide a concise answer.",
            compaction_prompt="Summarize the task in one short sentence without tools.",
            context_window=1_000,
            output_reserve=0,
        )
    )
    events = await event_summary(store, case_id)
    compacted = events["event_types"].get("compaction", 0)
    return {
        "case_id": case_id,
        "outcomes": [
            {
                "status": outcome.status.value,
                "turn": outcome.turn,
                "step": outcome.step,
                "error": outcome.error,
            }
        ],
        "tool_invocations": [],
        "delivered": [message.content for message in delivery.messages],
        "events": events,
        "compactions": compacted,
        "passed": outcome.status == "succeeded" and compacted >= 1,
    }


async def run_provider_retry(provider):
    case_id = "provider-retry"
    provider.fail_once_cases.add(case_id)
    result = await evaluate_case(
        provider, case_id, ["Reply with PROVIDER_RETRY_OK without tools."], config=config()
    )
    records = [record for record in provider.records if record["case_id"] == case_id]
    result["provider_errors"] = sum("error" in record for record in records)
    result["passed"] = (
        result["provider_errors"] == 1
        and result["outcomes"][0].get("status") == "succeeded"
        and any(record.get("attempt") == 1 for record in records)
    )
    return result


async def run_delivery_recovery(provider):
    case_id = "delivery-recovery"
    provider.case_id = case_id
    store = InMemoryEventStore()
    delivery = RecordingDelivery()
    original_deliver = delivery.deliver
    failures = 1

    async def fail_once(message):
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("Injected delivery failure")
        await original_deliver(message)

    delivery.deliver = fail_once
    loop = AgentLoop()
    tools = with_result_reads(build_tools([]), store)
    outcome = await loop.run(
        context(case_id, "Reply with DELIVERY_OK without tools.", store, provider, tools, delivery)
    )
    calls_before = len(provider.records)
    recovered = await loop.recover(case_id, store, delivery)
    return {
        "case_id": case_id,
        "outcomes": [{"status": outcome.status.value, "turn": outcome.turn, "step": outcome.step}],
        "tool_invocations": [],
        "delivered": [message.content for message in delivery.messages],
        "events": await event_summary(store, case_id),
        "recovered_ids": list(recovered.delivered_final_ids),
        "provider_calls_during_recovery": len(provider.records) - calls_before,
        "passed": (
            outcome.status == "delivery_unknown"
            and recovered.delivered_final_ids == (outcome.final_message_id,)
            and len(provider.records) == calls_before
        ),
    }


async def run_approval(provider):
    case_id = "approval-resume"
    provider.case_id = case_id
    invocations = []
    store = InMemoryEventStore()
    delivery = RecordingDelivery()
    tools = build_tools(invocations)
    tools.register(
        ToolSpec(
            "protected_write",
            "Write a protected test value",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            approval=ApprovalPolicy("eval-v1"),
        ),
        lambda arguments, ctx: (
            invocations.append({"name": "protected_write", "arguments": dict(arguments)})
            or Completed("protected write completed")
        ),
    )
    tools = with_result_reads(tools, store)
    loop = AgentLoop()
    pending = await loop.run(
        context(
            case_id,
            "Call protected_write with value='approved'. Do not call other tools.",
            store,
            provider,
            tools,
            delivery,
        )
    )
    approval = next(
        event
        for event in await store.load_recovery(case_id)
        if event.event_type == EventType.APPROVAL_REQUESTED
    )
    operation_id = approval.payload["items"][0]["operation_id"]
    resumed = await loop.resume(
        context(case_id, "Resume approved operation", store, provider, tools, delivery),
        decisions={operation_id: True},
    )
    return {
        "case_id": case_id,
        "outcomes": [
            {"status": pending.status.value, "turn": pending.turn, "step": pending.step},
            {"status": resumed.status.value, "turn": resumed.turn, "step": resumed.step},
        ],
        "tool_invocations": invocations,
        "delivered": [message.content for message in delivery.messages],
        "events": await event_summary(store, case_id),
        "passed": (
            pending.status == "approval_pending"
            and resumed.status == "succeeded"
            and len(invocations) == 1
        ),
    }


async def run_uncertain_retry(provider):
    case_id = "uncertain-reconcile"
    provider.case_id = case_id
    invocations = []
    started = True
    store = InMemoryEventStore()
    delivery = RecordingDelivery()
    tools = ToolRegistry()

    async def write(arguments, ctx):
        nonlocal started
        invocations.append({"name": "write_file", "arguments": dict(arguments)})
        if started:
            started = False
            await asyncio.sleep(1)
        return Completed("write reconciled")

    tools.register(
        ToolSpec(
            "write_file",
            "Write a test value with a short timeout",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            timeout_seconds=0.05,
            resources=("eval-workspace",),
            effect=ToolEffect.IDEMPOTENT,
        ),
        write,
    )
    loop = AgentLoop()
    blocked = await loop.run(
        context(
            case_id,
            "Call write_file once with path='recovery.txt' and content='test'.",
            store,
            provider,
            tools,
            delivery,
        )
    )
    uncertain = next(
        event
        for event in await store.load_recovery(case_id)
        if event.event_type == EventType.TOOL_RESULT
    )
    operation_id = uncertain.payload["operation_id"]
    reconciled = await loop.retry_operation(
        context(case_id, "Reconcile operation", store, provider, tools, delivery), operation_id
    )
    return {
        "case_id": case_id,
        "outcomes": [
            {"status": blocked.status.value, "turn": blocked.turn, "step": blocked.step},
            {"status": reconciled.status.value, "turn": reconciled.turn, "step": reconciled.step},
        ],
        "tool_invocations": invocations,
        "delivered": [message.content for message in delivery.messages],
        "events": await event_summary(store, case_id),
        "resource_locks": sorted(await loop.resource_locks(case_id, store)),
        "passed": (
            blocked.status == "blocked"
            and reconciled.status == "succeeded"
            and len(invocations) == 2
            and not await loop.resource_locks(case_id, store)
        ),
    }


async def run_cancellation(provider):
    case_id = "cancellation"
    provider.case_id = case_id
    invocations = []
    tool_started = asyncio.Event()
    store = InMemoryEventStore()
    delivery = RecordingDelivery()
    tools = ToolRegistry()

    async def read(arguments, ctx):
        invocations.append({"name": "read_file", "arguments": dict(arguments)})
        tool_started.set()
        await ctx.cancel.wait()
        return Cancelled(settled=True, partial_content="cancelled by evaluator")

    tools.register(
        ToolSpec(
            "read_file",
            "Wait until cancelled",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        read,
    )
    loop = AgentLoop()
    task = asyncio.create_task(
        loop.run(
            context(
                case_id,
                "Call read_file with path='cancel.txt', then wait for its result.",
                store,
                provider,
                tools,
                delivery,
            )
        )
    )
    await asyncio.wait_for(tool_started.wait(), timeout=90)
    accepted = await loop.cancel(case_id)
    outcome = await task
    events = await event_summary(store, case_id)
    return {
        "case_id": case_id,
        "outcomes": [{"status": outcome.status.value, "turn": outcome.turn, "step": outcome.step}],
        "tool_invocations": invocations,
        "delivered": [message.content for message in delivery.messages],
        "events": events,
        "cancel_accepted": accepted,
        "passed": accepted and outcome.status == "cancelled" and not events["unclosed_steps"],
    }


async def main():
    provider = RecordingProvider(
        model_id="kimi-k2.8-highspeed",
        base_url="https://free-tokens.msh.team/v1",
        api_key=os.environ["KIMI_API_KEY"],
        default_parameters={"max_tokens": 512},
        timeout_seconds=90,
    )
    cases = []
    started = time.monotonic()
    try:
        for result in await run_long_cases(provider):
            cases.append(result)
            print(progress(result), flush=True)
        for scenario in (
            run_paged_result,
            run_compaction,
            run_provider_retry,
            run_delivery_recovery,
            run_approval,
            run_uncertain_retry,
            run_cancellation,
        ):
            try:
                result = await scenario(provider)
            except Exception as exc:
                result = {
                    "case_id": scenario.__name__,
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            cases.append(result)
            print(progress(result), flush=True)
    finally:
        await provider.close()
    records = provider.records
    successful = [record for record in records if record["usage"] is not None]
    total_input = sum(record["usage"]["input_tokens"] or 0 for record in successful)
    total_cached = sum(record["usage"]["cached_input_tokens"] or 0 for record in successful)
    latencies = sorted(record["elapsed_seconds"] for record in successful)
    trace_ttft = sorted(
        value for case in cases for value in case.get("events", {}).get("model_ttft_ms", ())
    )
    summary = {
        "model": "kimi-k2.8-highspeed",
        "case_count": len(cases),
        "passed_cases": sum(case["passed"] for case in cases),
        "model_call_count": len(successful),
        "injected_provider_errors": len(records) - len(successful),
        "tool_call_count": sum(len(case.get("tool_invocations", [])) for case in cases),
        "input_tokens": total_input,
        "cached_input_tokens": total_cached,
        "cache_ratio": round(total_cached / total_input, 4) if total_input else 0,
        "output_tokens": sum(record["usage"]["output_tokens"] or 0 for record in successful),
        "provider_seconds": round(sum(record["elapsed_seconds"] for record in records), 3),
        "wall_seconds": round(time.monotonic() - started, 3),
        "median_provider_seconds": latencies[len(latencies) // 2] if latencies else 0,
        "p95_provider_seconds": latencies[max(0, int(len(latencies) * 0.95) - 1)]
        if latencies
        else 0,
        "timing": timing_summary(cases, records),
        "trace_ttft": {
            "samples": len(trace_ttft),
            "median_ms": trace_ttft[len(trace_ttft) // 2] if trace_ttft else None,
            "p95_ms": trace_ttft[max(0, int(len(trace_ttft) * 0.95) - 1)] if trace_ttft else None,
        },
    }
    destination = Path("docs/research/real-eval-2026-11-13/resilience-results.json")
    await asyncio.to_thread(
        destination.write_text,
        json.dumps(
            {"summary": summary, "provider_calls": records, "cases": cases},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    print(json.dumps({"summary": summary, "results": str(destination)}, indent=2), flush=True)


def progress(result):
    return json.dumps(
        {
            "case_id": result["case_id"],
            "passed": result["passed"],
            "outcomes": result.get("outcomes"),
            "tool_calls": len(result.get("tool_invocations", [])),
            "error": result.get("error"),
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    asyncio.run(main())
