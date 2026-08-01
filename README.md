# Agent Ops Kit

Evidence-gated operations for local AI agent stacks.

## Slice 1 - PR improvement canary

Discover actionable unresolved review threads on open pull requests, run a bounded economical build and review-fix pass in an isolated worktree, verify, push without force, and reply on the exact thread.

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
- One active job at a time; global circuit breaker on safety failures.
- No force push, merge, deploy, or automatic thread resolution.

### Tests

```bash
pytest
```
