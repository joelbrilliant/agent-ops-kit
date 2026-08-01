# Architecture (slice 1)

Public surface for Agent Ops Kit PR maintenance.

## Flow

1. `inspect` / `sweep` discovers open PRs via authenticated GitHub search (author + owned namespaces), dedupes, loads GraphQL review threads.
2. Actionable signals are unresolved, not outdated, on the observed head SHA, with latest external comment from a trusted reviewer.
3. SQLite atomically claims `repo + PR + thread + latest comment + head SHA`. A partial unique index permits one active job globally. Pre-mutation interruptions can be reclaimed. Mutation-ambiguous interruptions open the global circuit for manual reconciliation.
4. A fresh Oscar session classifies the untrusted review packet as ROUTINE or HOLD. Its exact route and session identity are attested.
5. For ROUTINE, the builder continues the classifier session with the same attested session ID and continuation token. It commits a bounded candidate in a worktree pinned to the observed SHA.
6. Named verification runs, then a second fresh and distinct Oscar session adversarially reviews the exact candidate SHA, fixes every in-scope finding, writes the Joel-voice reply draft, and attests the voice gate. The complete named verification set runs again against the resulting SHA.
7. Immediately before mutation, the orchestrator revalidates the exact thread state and latest comment, PR head SHA and repositories, head-repository push permission, required checks, remote head ref, clean worktree, and history ancestry.
8. Push targets the attested head repository and existing head ref without force. The remote resulting SHA is read back before the exact-thread GraphQL reply is posted.
9. Reply readback must match both the returned node ID and exact drafted body. The portable receipt is redacted and rejects raw bodies, credentials, private paths, transcripts, and operator fixtures.

## Session and capability boundary

Each job has exactly two fresh Oscar sessions. Classification and build are one session. Review-fix is the second. The external runners return strict response schemas with exact route, freshness, session, and SHA attestations. The orchestrator rejects missing or extra fields.

Actionable PR notifications share the same one-active-job ledger. Trusted
inline review threads use the existing classifier/build/reviewer sweep. CI and
top-level PR notifications use one bounded `NotificationWorkerResponseV1`; a
code change then passes through the same independent reviewer and final
verification gate. GitHub credentials, pushes, comments, readback, mark-read and
Buzz delivery remain in the deterministic orchestrator. No detached chat
process or mutable job JSON controls lifecycle state.

Oscar gets no orchestrator GitHub credential capability. Runner processes receive an allowlisted environment, isolated `HOME` and `GH_CONFIG_DIR`, disabled Git credential helpers, and no inherited GitHub token variables. The orchestrator separately proves its own GitHub identity and that `gh auth status` fails in the runner environment before launching Oscar.

## Sweep semantics

Search covers every configured operator login and owned namespace with fail-closed REST pagination. Review-thread and per-thread comment connections are independently paginated. A sweep processes every eligible claim serially and refreshes discovery after each successful job. Pause and open-circuit states still inspect for operator visibility but do not claim work, launch runners, create worktrees, push, or reply.

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
