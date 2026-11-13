# ADR-0011: Live Provider acceptance for 1.0

Status: accepted

## Context

The earlier plan required official OpenAI and Anthropic credentials for 1.0, but they were unavailable. The maintainer selected DeepSeek as the live Provider acceptance target instead.

Recorded OpenAI and Anthropic SSE tests verify Adapter protocol behavior. They are not acceptance tests against the official hosted APIs.

## Decision

- The 1.0 live gate covers DeepSeek text, Tool Calling, Preview, usage, Delivery, Outbox export, and Session deletion.
- All existing Store, ToolRuntime, DeliveryOwner, recovery, fault-injection, import, benchmark, Python 3.11–3.14, package, and clean-install gates remain required.
- Recorded OpenAI and Anthropic streams remain Adapter evidence. Official OpenAI and Anthropic API acceptance is not claimed and is not a 1.0 blocker.
- Future official Provider acceptance can be added separately without changing the 1.0 runtime contracts.

## Consequences

Version 1.0.0 freezes the tested public runtime contracts without claiming that every built-in Provider was tested against its official hosted API. The release evidence names the actual live Provider and does not generalize DeepSeek results to other services.
