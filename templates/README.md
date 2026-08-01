# Deployment templates

`com.joelbrilliant.github-watch.plist` is deliberately disabled and runs every three hours when enabled after independent PASS. Copy and adjust the paths only during an approved live cutover.

`config.json` is the complete local configuration shape. Replace executable paths and namespace values with the reviewed local values before use. It contains no secrets or shell commands.

`oscar-oneshot` is a versioned wrapper template for the installed Hermes CLI. It sets the Oscar profile home and invokes the supported `hermes --oneshot PROMPT` contract. Install it at the configured executable path only during an approved live cutover.

`github-watch-buzz-notify` is a versioned wrapper template for Buzz. It accepts `MESSAGE [CHANNEL_UUID]`, loads only `BUZZ_*` entries from its local credentials file, invokes `buzz messages send --channel UUID --content TEXT`, and exits non-zero unless Buzz returns JSON with `accepted: true`. Install it at the configured executable path only during an approved live cutover.

The packaged `github_watch/templates/oscar-headless.sb` profile is the Oscar launch contract. It allows normal command execution but denies `/usr/bin/open` and application-bundle executables. The worker always launches the configured wrapper through `/usr/bin/sandbox-exec` with `CI=1` and no display variables.
