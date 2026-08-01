# Configuration

Local JSON only. Never commit real config or state directories.

## Required keys

- `operator_logins` - GitHub logins treated as the authenticated operator
- `owned_namespaces` - user or org namespaces whose open PRs are inspected
- `trusted_reviewer_logins` - logins whose latest thread comments are actionable
- `workspace_root` - directory for mirrors and worktrees
- `state_dir` - ledger, receipts, request files
- `classifier_command` - argv array for the read-only classifier
- `builder_command` - argv array for the economical builder
- `reviewer_command` - argv array for the Sol review-fix runner

## Optional keys

- `excluded_repositories` - full `owner/name` list (default empty)
- `protected_path_patterns` - always blocked paths
- `default_verification_commands` - map of check id to argv array
- `repository_policies` - per-repo `permitted_paths` and `verification_commands`
- `notification_mode` - `quiet` | `concise` | `verbose`
- `reclaim_after_seconds` - interrupted job reclaim threshold (default 6h)
- `runner_timeout_seconds` - external runner timeout
- `pause_file_name` - relative name under `state_dir` (default `PAUSED`)
- `gh_command` / `git_command` - binary names

## Runner placeholders

Only these fixed placeholders are expanded in command arrays:

- `{request_path}` - owner-only JSON request file
- `{response_path}` - runner JSON response file
- `{worktree_path}` - isolated worktree (builder/reviewer)

Untrusted review text is never interpolated into argv. It is written only into the request file.

## Example

See `config.example.json` at the repository root. Copy it outside the repo, fill paths and runner commands, and pass `--config` to the CLI.
