from __future__ import annotations

from importlib import import_module

import pytest

import nextloop

EXPECTED_ROOT_EXPORTS = frozenset(
    {
        "API_VERSION",
        "AgentLoop",
        "AnthropicProvider",
        "ApprovalPolicy",
        "Cancelled",
        "CandidateRequest",
        "CapacityError",
        "Completed",
        "CompositeToolRuntime",
        "DeliveryOwner",
        "EVENT_SCHEMA_VERSION",
        "Event",
        "EventStore",
        "EventType",
        "FinalMessage",
        "FinishReason",
        "HARNESS_SCHEMA",
        "Harness",
        "HarnessError",
        "HarnessSpec",
        "InMemoryEventStore",
        "InvariantViolation",
        "LoopConfig",
        "McpStdioRuntime",
        "MemoryDeliveryOwner",
        "Message",
        "ModelCallPurpose",
        "ModelCallStatus",
        "ModelCallTrace",
        "ModelProvider",
        "ModelResponse",
        "ModelUsage",
        "NewEvent",
        "NextloopError",
        "ObservingEventStore",
        "OpenAIProvider",
        "PendingInput",
        "PayloadErasedError",
        "PreviewEvent",
        "PreviewKind",
        "PreviewSink",
        "ProtocolExhausted",
        "ProviderConfig",
        "ProviderError",
        "RecoveryOutcome",
        "SQLiteEventStore",
        "STDIO_PROTOCOL_VERSION",
        "SkillConfig",
        "SkillDocument",
        "SkillError",
        "SkillRegistry",
        "SkillSpec",
        "Skipped",
        "StdioDriver",
        "StdioProtocolError",
        "StdoutExporter",
        "StoreConfig",
        "SteerResult",
        "SteerStatus",
        "SurfaceOp",
        "TRACE_SCHEMA",
        "TimingSource",
        "ToolCall",
        "ToolCallTrace",
        "ToolConfig",
        "ToolContext",
        "ToolEffect",
        "ToolExecutionMode",
        "ToolExecutionTiming",
        "ToolOutcome",
        "ToolOutcomeKind",
        "ToolRegistry",
        "ToolResultMode",
        "ToolRuntime",
        "ToolSpec",
        "TraceConfig",
        "TraceExporter",
        "TraceExporterInput",
        "TraceProjector",
        "TraceRecord",
        "TurnContext",
        "TurnOutcome",
        "TurnStatus",
        "TurnTrace",
        "TurnTraceStatus",
        "UncertainHanging",
        "UncertainSettled",
        "ValidatedCall",
        "ValidationIssue",
        "WriterLeaseError",
        "check_event_store",
        "load_harness_spec",
        "new_id",
        "project_traces",
        "skill_view",
        "utc_now",
        "with_result_reads",
    }
)


def test_root_reexports_match_the_frozen_contract():
    assert frozenset(nextloop.__all__) == EXPECTED_ROOT_EXPORTS
    assert len(nextloop.__all__) == len(EXPECTED_ROOT_EXPORTS)
    assert all(hasattr(nextloop, name) for name in EXPECTED_ROOT_EXPORTS)


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        ("nextloop.contracts", ("EventStore", "ModelProvider", "TurnContext")),
        ("nextloop.loop", ("AgentLoop",)),
        ("nextloop.events", ("project_events", "project_traces")),
        ("nextloop.adapters", ("InMemoryEventStore", "SQLiteEventStore")),
        ("nextloop.execute", ("ToolRegistry", "BatchExecutor")),
        ("nextloop.harness", ("Harness",)),
        ("nextloop.skills", ("SkillRegistry", "skill_view")),
        (
            "nextloop.testing",
            (
                "FaultInjectingEventStore",
                "FaultInjectingToolRuntime",
                "FaultInjectingDeliveryOwner",
                "check_event_store",
            ),
        ),
    ],
)
def test_core_and_extra_module_reexports(module_name, names):
    module = import_module(module_name)
    for name in names:
        assert hasattr(module, name)
        if hasattr(nextloop, name):
            assert getattr(module, name) is getattr(nextloop, name)


@pytest.mark.parametrize(
    ("module_name", "expected"),
    [
        (
            "nextloop.events",
            {
                "BatchAction",
                "BatchDecision",
                "Projection",
                "TraceProjector",
                "RepairDecision",
                "ResponseAction",
                "ResponseDecision",
                "decide_response",
                "project_events",
                "project_traces",
                "repair_events",
                "settle_outcomes",
                "upcast_event",
            },
        ),
        (
            "nextloop.adapters",
            {
                "AnthropicProvider",
                "InMemoryEventStore",
                "ObservingEventStore",
                "OpenAIProvider",
                "SQLiteEventStore",
                "StdoutExporter",
            },
        ),
        (
            "nextloop.execute",
            {
                "BatchExecutor",
                "BatchResult",
                "ModelCaller",
                "ModelCallResult",
                "ToolContext",
                "ToolRegistry",
                "ValidatedCall",
                "ValidationIssue",
            },
        ),
        (
            "nextloop.testing",
            {
                "DuplicateEventIdStore",
                "FaultInjectingDeliveryOwner",
                "FaultInjectingEventStore",
                "FaultInjectingToolRuntime",
                "InjectedCrash",
                "UncheckedWriterStore",
                "WrongSurfaceStore",
                "check_event_store",
            },
        ),
    ],
)
def test_complete_module_reexport_contract(module_name, expected):
    module = import_module(module_name)
    assert frozenset(module.__all__) == expected
    assert len(module.__all__) == len(expected)
