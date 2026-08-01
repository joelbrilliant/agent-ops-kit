# Configuration

Local JSON only. Never commit real config or state directories.

## Required keys

- `operator_logins` - GitHub logins treated as the authenticated operator
- `owned_namespaces` - user or org namespaces whose open PRs are inspected
- `trusted_reviewer_logins` - logins whose latest thread comments are actionable
- `workspace_root` - directory for mirrors and worktrees
- `state_dir` - ledger, receipts, request files
- `classifier_command` - argv array for the read-only classifier
- `builder_command` - argv array for the same-session builder continuation
- `reviewer_command` - argv array for the fresh Oscar reviewer-fixer
- `required_runner_identity` - exact profile, provider, model, reasoning effort, and service tier attestation
- `capability_isolation` - must set `enabled: true`; declares the runner environment allowlist and private redaction markers

## Optional keys

- `excluded_repositories` - full `owner/name` list (default empty)
- `trusted_reviewer_associations` - GitHub associations trusted for maintainer comments: `OWNER`, `MEMBER`, and `COLLABORATOR` (default empty)
- `protected_path_patterns` - always blocked paths
- `default_verification_commands` - map of check id to argv array
- `repository_policies` - per-repo `permitted_paths` and `verification_commands`; `*` is an optional fallback for unlisted repositories, while an exact repository entry wins and is not merged with the fallback
- `notification_mode` - `quiet` | `concise` | `verbose`
- `reclaim_after_seconds` - interrupted job reclaim threshold (default 6h)
- `runner_timeout_seconds` - external runner timeout
- `pause_file_name` - relative name under `state_dir` (default `PAUSED`)
- `gh_command` / `git_command` - binary names
- `issue_automation` - optional issue-to-draft-PR automation (see below)

## Issue automation

Optional `issue_automation` object. When present it must name exact repositories that already have exact `repository_policies` entries (wildcard-only policy is invalid).

Required shape:

- `enabled` - boolean master switch
- `enabled_repositories` - map of exact `owner/name` to base branch
- `require_labels` or `trigger_label` - maintainer-only trigger label(s); default via `trigger_label` is `agent-ops:ready`
- `branch_prefix` - safe ref prefix (default `agent-ops/issue`)
- `classifier_command` / `builder_command` / `reviewer_command` - argv templates with the same fixed placeholders as PR automation
- `build_runner_identity` / `review_runner_identity` - distinct attested runner identities for sticky build-profile classify+build and fresh review-profile review-fix

Optional bounds and templates:

- `ignore_labels`
- `draft_pr_title_template` / `issue_reply_template`
- `max_paths_per_issue` / `max_changed_files` / `max_diff_lines`

Issue jobs claim through the same global ledger as PR jobs, so only one active job exists at a time. Raw issue title, body and comments are owner-only request data; public inspect output and receipts carry digests only.

CLI:

- `agent-ops issue inspect --config PATH`
- `agent-ops issue sweep --config PATH`

Issue automation never merges, closes issues, mutates labels, force-pushes, or changes repository settings. The normal success path is one verified draft PR plus one verified issue reply.

## Runner placeholders

Only these fixed placeholders are expanded in command arrays:

- `{request_path}` - owner-only JSON request file
- `{response_path}` - runner JSON response file
- `{worktree_path}` - isolated worktree (builder/reviewer)

Untrusted review text is never interpolated into argv. It is written only into the request file.

## Runner contract

The classifier must start a fresh session and return `ClassifierResponseV1` with a continuation token and exact runner identity. The builder must continue that session, set `fresh_session` to false, attest the continuation-token digest, and bind its response to the exact base and resulting SHAs. The reviewer must start a second fresh session, distinct from the classifier session, and return `ReviewerResponseV1` bound to the candidate and resulting SHAs.

For issue automation the build-profile identity is required on classifier and builder responses, and the review-profile identity is required on `IssueReviewerResponseV1` (PASS plus draft PR title/body and issue reply draft after the Joel voice gate).

Responses use exact schemas. Missing keys, extra keys, route mismatches, session mismatches, stale SHAs, unresolved findings, or failed voice attestation stop the job and open the global circuit before any push.

The reviewer response includes a short public-community reply draft and `VoiceGateV1`. The gate attests that the shared operator contract, operator profile, `joel-voice-writing`, and `references/voice.md` were loaded. The draft must include the full resulting SHA and every named check.

## Capability isolation

Oscar subprocesses receive a new local `HOME`, empty Git credential configuration, a new `GH_CONFIG_DIR`, and only environment variables named in `capability_isolation.environment_allowlist`. Do not add GitHub tokens, credential-helper variables, operator home paths, or messaging credentials to that allowlist. Before any runner starts, the orchestrator proves that `gh auth status` fails inside this environment while separately attesting the orchestrator's GitHub identity.

`capability_isolation.private_markers` should contain stable local path or operator-specific fragments that must never appear in receipts or public reply drafts. Do not put credentials in config.

## Verification policy

Every command in `default_verification_commands` and the matching repository policy runs after the builder and again after the reviewer-fixer. A classifier may request additional configured check IDs but cannot invent commands. Unknown IDs fail closed. Repository policies must provide `permitted_paths`; a review comment cannot expand its own path scope. Issue jobs always use the exact configured repository policy paths and never widen from issue text.

## Example

See `config.example.json` at the repository root. Copy it outside the repo, fill paths and runner commands, and pass `--config` to the CLI.
