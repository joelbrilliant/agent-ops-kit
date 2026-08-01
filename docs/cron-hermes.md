# Hermes three-hour cron example

Run one complete sweep every three hours. The cheap poll launches no model when no new signal exists.

## Local-only job (save output, no chat delivery)

```text
hermes cron create "0 */3 * * *" \
  --name agent-ops-pr-sweep \
  --deliver local \
  --workdir "$HOME/Projects/agent-ops-kit" \
  'Run agent-ops pr sweep --config "$HOME/.config/agent-ops/config.json". Exit 0 is success or no work. Exit 1 is held or circuit open - report status with agent-ops pr status --config "$HOME/.config/agent-ops/config.json". Exit 2 is config or tooling failure.'
```

## Manual run-now

```bash
agent-ops pr sweep --config "$HOME/.config/agent-ops/config.json"
agent-ops pr inspect --config "$HOME/.config/agent-ops/config.json"
agent-ops pr status --config "$HOME/.config/agent-ops/config.json"
agent-ops issue inspect --config "$HOME/.config/agent-ops/config.json"
agent-ops issue sweep --config "$HOME/.config/agent-ops/config.json"
```

## Global pause / disable

```bash
agent-ops pr pause --config "$HOME/.config/agent-ops/config.json"
# later
agent-ops pr resume --config "$HOME/.config/agent-ops/config.json"
```

Issue automation shares the same pause file, ledger lock, and circuit breaker as the PR loop.

After a safety failure the ledger circuit breaker opens. Diagnose, then:

```bash
agent-ops pr clear-circuit --config "$HOME/.config/agent-ops/config.json"
```

Paused or circuit-open sweeps still inspect account-wide PR or issue state, then stop without claiming work, launching Oscar, creating worktrees, pushing, or replying.
