# Architecture (slice 1)

Public surface for Agent Ops Kit PR maintenance.

## Flow

1. `inspect` / `sweep` discovers open PRs via authenticated GitHub search (author + owned namespaces), dedupes, loads GraphQL review threads.
2. Actionable signals are unresolved, not outdated, on the observed head SHA, with latest external comment from a trusted reviewer.
3. SQLite ledger atomically claims `repo + PR + thread + latest comment + head SHA`. One active job. Circuit breaker on safety failures.
4. Classifier runner (argv) returns `DecisionV1` ROUTINE or HOLD before any mutable worktree.
5. For ROUTINE: prove push permission, create isolated worktree at exact head SHA, run builder, path-bound check, named verification, Sol reviewer, re-verify.
6. Re-fetch thread and head immediately before non-force push to existing head ref.
7. Exact-thread GraphQL reply + readback. Receipt is redacted (no raw bodies, credentials, private paths).

## Modules

- `agent_ops.cli` - `pr inspect|sweep|status|pause|resume|clear-circuit`
- `agent_ops.config` - local JSON config
- `agent_ops.contracts` - SignalV1, DecisionV1, TaskSpecV1, ActionReceiptV1
- `agent_ops.github` - gh client, discovery, reply
- `agent_ops.maintenance` - ledger, worktree, orchestrator
- `agent_ops.runners` - external runner contract
- `agent_ops.qa` - named verification
- `agent_ops.audit` - redaction + receipts
- `agent_ops.process` - `shell=False` subprocess only

## Non-goals

No email, webhooks, auto-merge, deploy, memory kit, or private harness paths in the public core.
