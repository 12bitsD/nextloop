# ADR-0011: Live Provider acceptance for 1.0

Status: accepted

## Context

The earlier plan required official OpenAI and Anthropic API credentials for 1.0. Those credentials are unavailable. The maintainer explicitly selected DeepSeek as the live Provider acceptance target instead. Recorded OpenAI and Anthropic SSE tests already cover their Adapter protocol behavior but are not official API acceptance.

## Decision

- DeepSeek live text, Tool Calling, Preview, usage, Delivery, Outbox export, and Session deletion scenarios form the 1.0 live Provider gate.
- All existing Store, ToolRuntime, DeliveryOwner, recovery, fault-injection, import, benchmark, Python 3.11–3.14, package, and clean-install gates remain mandatory.
- Recorded OpenAI and Anthropic streams remain Adapter evidence. Official OpenAI and Anthropic API acceptance is not claimed and is not a 1.0 blocker under this decision.
- Future official Provider acceptance can be added as separate evidence without changing the 1.0 runtime contracts.

## Consequences

Version 1.0.0 freezes the tested public runtime contracts without asserting that every built-in Provider has been exercised against its official hosted API. The release evidence names the actual live Provider and does not generalize DeepSeek results to other hosted services.
