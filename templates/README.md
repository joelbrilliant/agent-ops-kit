# Deployment templates

`com.joelbrilliant.github-watch.plist` runs every three hours. Before loading it during an approved cutover, install the reviewed package and create the runtime's `paused` sentinel with `github-watch pause`. A plist `Disabled` key is not pause evidence. Prove the service is loaded and the sentinel still blocks mutation.

`config.json` is the complete local configuration shape. Replace executable paths and namespace values with the reviewed local values before use. It contains no secrets or shell commands.

`oscar-oneshot` is a versioned wrapper template for the installed Hermes CLI. It sets the Oscar profile home and invokes the supported `hermes --oneshot PROMPT` contract using the reviewed absolute executable path. Install it at the configured executable path only during an approved live cutover.

`github-watch-buzz-notify` is a versioned wrapper template for Buzz. It accepts `MESSAGE [CHANNEL_UUID]`, safely removes matching quotes from local `BUZZ_*` values, gives Buzz a minimal child environment, invokes `buzz messages send --channel UUID --content TEXT`, and exits non-zero unless Buzz returns JSON with `accepted: true`. Install it at the configured executable path only during an approved live cutover.

The `oscar` directory versions the reviewed profile contracts that replace the stale live Agent Ops profile. `hermes-github-watch-clause.md` is the reviewed replacement for the automated GitHub section in the shared Hermes contract.

The packaged `github_watch/templates/oscar-headless.sb` and `oscar-readonly.sb` profiles are the worker launch contracts. Both deny `/usr/bin/open`, application-bundle executables and unrelated credential stores. Read-only triage also denies GitHub and SSH credential stores plus Keychain execution. The worker always launches the configured wrapper through `/usr/bin/sandbox-exec` with `CI=1`, a minimal environment and no display variables.
