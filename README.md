# Agent Ops Kit

Evidence-gated operations for local AI agent stacks.

## Slice 1 - PR improvement canary

Discover every actionable unresolved review thread across the configured GitHub account, process each job serially through two attested Oscar sessions, verify the exact resulting commit, push without force, and reply on the exact thread.

### Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### CLI

```bash
agent-ops pr inspect --config /path/to/config.json
agent-ops pr sweep --config /path/to/config.json
agent-ops pr status --config /path/to/config.json
agent-ops pr pause --config /path/to/config.json
agent-ops pr resume --config /path/to/config.json
agent-ops pr clear-circuit --config /path/to/config.json
```

Exit codes: `0` no work or completed work, `1` held job or open circuit, `2` invalid config or missing tooling.

### Configuration

Local JSON only (gitignored). See `docs/configuration.md` and `config.example.json`.

### Schedule

A three-hour Hermes cron example is in `docs/cron-hermes.md`.

### Safety

- Untrusted GitHub text is written only to owner-only request files or stdin.
- Runner commands are argv arrays with fixed placeholders only.
- Subprocess calls always use `shell=False`.
- Oscar runner environments inherit no GitHub credentials, operator home, or messaging capability.
- Classifier and builder share one session. Reviewer-fixer uses a second fresh session.
- Strict runner schemas attest exact route, session identity, candidate SHA, resulting SHA, and reply voice gate.
- One active job at a time with atomic deduplication, interruption recovery, and a global circuit breaker.
- Paused and circuit-open sweeps inspect only.
- Thread, head SHA, head repository permission, required checks, remote ref, clean worktree, and history are revalidated immediately before mutation.
- Pushes target the attested head repository and existing head ref without force.
- No merge, deploy, repository-setting change, force push, or automatic thread resolution.

### Tests

```bash
python3 -m pytest -q
python3 -m build
```
