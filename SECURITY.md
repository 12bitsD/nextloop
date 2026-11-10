# Security policy

## Reporting

Report suspected vulnerabilities through [GitHub private security advisories](https://github.com/12bitsD/nextloop/security/advisories/new). Do not open a public issue with exploit details, credentials, or private Session data.

Include the affected version or commit, a minimal reproduction, the security impact, and the required attacker capability. Remove credentials and personal data from logs and Trajectories.

## Trust model

- `harness.toml` is trusted configuration. It can import arbitrary Python modules and start local worker processes. Do not load untrusted Harness configuration.
- Tool handlers and stdio/MCP workers execute with the host process's permissions. nextloop does not provide a sandbox or an authorization policy.
- Provider base URLs and credentials are caller configuration. Event payloads and Trajectories can contain sensitive model, user, and Tool data.
- SQLite durability does not replace filesystem permissions, backups, encryption, or retention controls.
- Delivery is at least once. Delivery owners must deduplicate by `final_message_id`; Tool callers remain responsible for external idempotency.
- Trace Exporters are caller-provided side effects. Exported copies and third-party retention are outside the EventStore's control.

Current releases provide optional same-host time-based writer leases and persistent Trace Outbox state. They do not provide distributed writer coordination, Session deletion, remote MCP/OAuth, or event-level PII erasure. Do not represent those planned capabilities as existing controls.
