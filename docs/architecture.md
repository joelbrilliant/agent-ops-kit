# Architecture

Public surface for Agent Ops Kit PR maintenance and issue-to-draft automation.

## PR improvement flow (slice 1)

1. `inspect` / `sweep` discovers open PRs via authenticated GitHub search (author + owned namespaces), dedupes, loads GraphQL review threads.
2. Actionable signals are unresolved, not outdated, on the observed head SHA, with latest external comment from a trusted reviewer.
3. SQLite atomically claims `repo + PR + thread + latest comment + head SHA`. A partial unique index permits one active job globally. Pre-mutation interruptions can be reclaimed. Mutation-ambiguous interruptions open the global circuit for manual reconciliation.
4. A fresh Oscar session classifies the untrusted review packet as ROUTINE or HOLD. Its exact route and session identity are attested.
5. For ROUTINE, the builder continues the classifier session with the same attested session ID and continuation token. It commits a bounded candidate in a worktree pinned to the observed SHA.
6. Named verification runs, then a second fresh and distinct Oscar session adversarially reviews the exact candidate SHA, fixes every in-scope finding, writes the Joel-voice reply draft, and attests the voice gate. The complete named verification set runs again against the resulting SHA.
7. Immediately before mutation, the orchestrator revalidates the exact thread state and latest comment, PR head SHA and repositories, head-repository push permission, required checks, remote head ref, clean worktree, and history ancestry.
8. Push targets the attested head repository and existing head ref without force. The remote resulting SHA is read back before the exact-thread GraphQL reply is posted.
9. Reply readback must match both the returned node ID and exact drafted body. The portable receipt is redacted and rejects raw bodies, credentials, private paths, transcripts, and operator fixtures.

## Issue-to-draft flow (slice 3)

1. `issue inspect` / `issue sweep` discovers open labelled issues in explicitly enabled repositories with exact repository policies.
2. The portable `IssueSignalV1` surface stores repository and issue identity, title/body/conversation digests, observed timestamp, latest comment node ID, configured base ref and observed base SHA. Raw title/body/comments remain untrusted and appear only in owner-only runner request files.
3. Classification uses a sticky build-profile session before any worktree exists. HOLD writes a receipt and stops.
4. ROUTINE creates a new branch from the observed configured base-ref tip, runs the sticky builder, verifies, then a distinct fresh review-profile session may fix in-bounds and produce draft PR copy plus the issue reply draft.
5. Bounds include path policy, protected paths, `max_paths_per_issue`, `max_changed_files`, and `max_diff_lines`.
6. Immediately before push the orchestrator revalidates the exact repository and path/check policy, operator ownership, public visibility, push permission, open state, trigger labels, conversation digest, base tip SHA, clean bounded reviewer SHA, and absent target branch.
7. Non-force push, draft PR create (`draft=true`, body includes `Closes #N`), and issue comment all require exact readback. No merge, ready-for-review, label mutation, or issue close.
8. Claim key is repository + issue number + conversation digest + base tip SHA. A completed issue is terminal even if a later comment changes the digest, preventing duplicate draft PRs. Held pre-mutation conversations may be reconsidered only when their digest changes. Issue and PR loops share the global active-job lock, pause file, and circuit breaker.

## Session and capability boundary

Each job has exactly two fresh sessions. Classification and build are one sticky session. Review-fix is the second distinct session. The external runners return strict response schemas with exact route, freshness, session, and SHA attestations. The orchestrator rejects missing or extra fields.

Runner processes do not inherit orchestrator GitHub credentials through their environment or Git configuration. Build and review profiles receive separate allowlisted environments, isolated `HOME` and `GH_CONFIG_DIR`, disabled Git credential helpers, and no inherited GitHub or messaging credential variables. The orchestrator separately proves its own GitHub identity and proves that `gh auth status` fails in each environment before and after its runner boundary.

## Sweep semantics

Search covers every configured operator login and owned namespace with fail-closed REST pagination. Review-thread and per-thread comment connections are independently paginated. A sweep processes eligible claims serially. Pause and open-circuit states do not claim work, launch runners, create worktrees, push, or reply; the explicit inspect command remains available for operator visibility. Disabled issue automation still allows inspect and never claims.

## Modules

- `agent_ops.cli` - `pr|issue inspect|sweep|status|pause|resume|clear-circuit`
- `agent_ops.config` - local JSON config including optional `issue_automation`
- `agent_ops.contracts` - SignalV1, IssueSignalV1, DecisionV1, TaskSpecV1, ActionReceiptV1, IssueDraftReceiptV1
- `agent_ops.github` - gh client, PR discovery/reply, issue discovery, draft PR, issue comment
- `agent_ops.maintenance` - ledger, worktree, PR orchestrator, issue orchestrator
- `agent_ops.runners` - external runner contract including IssueReviewerResponseV1
- `agent_ops.qa` - named verification
- `agent_ops.audit` - redaction, receipts and read-only deterministic public receipt summaries
- `agent_ops.process` - `shell=False` subprocess only

## Non-goals

No email, webhooks, auto-merge, deploy, memory kit, or private harness paths in the public core. Issue automation does not merge, close issues, mutate labels, force-push, or change repository settings.

Slice 3 is not a general-autonomy claim. Portable evidence must be followed by the frozen packet's live canary sequence before operators enable issue automation.

An audit report is a redacted local receipt summary. It excludes local receipt names and paths, raw request and response material, branch names and hold reasons. It is evidence that accepted receipt files validated, not proof of autonomous correctness or live readiness.
