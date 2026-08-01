### Automated GitHub Watch lane

- A model-free GitHub Watch polls unread pull request notifications every three hours and launches no worker for provably obsolete work.
- Every remaining notification launches one fresh bounded Oscar one-shot session that owns routine inspection, narrow repair, targeted tests, normal push and one exact-PR reply end to end.
- Non-owned pull requests are read-only triage. Mutation is allowed only for Joel-authored pull requests or configured namespaces where live repository permissions prove push access.
- GitHub Watch independently verifies the final remote head and Joel-authored exact-PR reply before mark-read, then sends one accepted Buzz completion. Blocked work remains unread and sends one accepted blocker with a proposed fix.
- Oscar never merges, force-pushes, deploys, changes settings or credentials, weakens security, starts desktop applications, delegates or expands scope.
- The scheduler remains paused until Joel explicitly approves the first live mutation run.
