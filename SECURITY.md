# Security policy

## Reporting

Report suspected vulnerabilities through [GitHub private security advisories](https://github.com/12bitsD/nextloop/security/advisories/new). Do not open a public issue with exploit details, credentials, or private Session data.

Include:

- the affected version or commit;
- a minimal reproduction;
- the security impact;
- the attacker capability required.

Remove credentials and personal data from logs and Trajectories.

## Trust model

- `harness.toml` is trusted configuration. It can import arbitrary Python modules and start local worker processes. Do not load untrusted Harness configuration.
- Tool handlers and stdio/MCP workers run with the host process's permissions. nextloop does not provide a sandbox or a general host authorization policy.
- Provider base URLs and credentials are caller configuration. Event payloads and Trajectories can contain sensitive model, user, and Tool data.
- SQLite durability does not replace filesystem permissions, backups, encryption, or retention controls.
- Delivery is at least once. Delivery owners must deduplicate by `final_message_id`; Tool callers remain responsible for external idempotency.
- Trace Exporters are caller-provided side effects. Exported copies and third-party retention are outside the EventStore's control.

Current releases provide optional same-host writer leases, persistent Trace Outbox state, and reentrant whole-Session deletion for local Event, Trace, and Outbox state.

They do not provide distributed writer coordination, deletion of third-party exports or backups, external-asset lifecycle, remote MCP/OAuth, or Event-level PII erasure. Do not describe those excluded capabilities as existing controls.
