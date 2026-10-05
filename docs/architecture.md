# Architecture and operations

## Process model

There are up to two long-running application processes in headless mode:

1. `telegram_bridge.runtime serve` starts the official native sidecar on `127.0.0.1` with private XDG directories.
2. `bot.py` polls Telegram and talks to that sidecar through the native CLI’s authenticated API client.

Desktop mode skips the owned native service and discovers an existing authenticated loopback sidecar. It never reads desktop credentials or starts a replacement desktop account.

## State and recovery

Updates are claimed and the polling offset is committed before dispatch. Mutation calls are not automatically retried after a timeout, HTTP 5xx, malformed response, or uncertain Telegram delivery. The operator can inspect `/status`, `/latest`, and the native OmniRush UI before intentionally repeating work.

Thread metadata is private and contains only bot-created session IDs, project paths, policy mode, timestamps, and delivery claims. Thread selection verifies both the bot-owned ID and its native project directory before making an API call.

## systemd user service

`python3 omnirush.py service install --enable` writes marked units under `~/.config/systemd/user`. It uses `UMask=0077`, loopback headless binding, restart backoff, and control-group shutdown. It never overwrites an unrelated unit. `--linger` is an explicit optional request; it is not a guarantee of uptime.

If systemd user management is unavailable, `start` uses a verified detached supervisor and `run` is the foreground option. The fallback is intentionally not described as reboot-persistent or always-on.

## Release verification

The rootless runtime installer only accepts explicitly pinned official assets from the OmniRush GitHub release, verifies the GitHub API metadata, exact size, exact SHA-256, safe tar/gzip member names, ELF architecture, and private install manifest. It does not run archive scripts, use sudo, install globally, follow arbitrary proxies, or overwrite a prior version.

## Threat model

The bot is an authenticated remote interface to the configured Linux user. Owner-only private chat validation is defense in depth, not a substitute for OS isolation. Configure a dedicated user/project root, keep the Telegram token private, review `ASK` approvals, and treat `FULL` as equivalent to granting the agent tool access available to that user.
