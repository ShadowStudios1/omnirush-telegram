# OmniRush Telegram Portable 🚀

An owner-only Telegram control plane for OmniRush on Linux, WSL, Linux RDP desktops, containers, and cloud VMs.

The project is deliberately **stdlib-only**. It detects the host, guides setup in an animated terminal wizard, keeps credentials outside the repository, runs the official OmniRush native sidecar in a private user directory when headless mode is selected, and supervises the Telegram bridge with systemd user services or an explicit foreground/detached fallback.

> **Security first:** this is remote access to an AI coding agent running as your Linux user. Telegram chats are not end-to-end encrypted. The safe default is `ASK`; `FULL` is a deliberate per-session permission policy and is never a root, OS, organization, or website-approval bypass.

## Fast setup

Use a dedicated unprivileged Linux user on an always-on machine for production. Do not run this project as root and never put a Telegram token in a command argument, issue, screenshot, or chat.

```bash
git clone https://github.com/ShadowStudios1/omnirush-telegram.git omnirush-telegram
cd omnirush-telegram
./setup.sh
```

`setup.sh` is a no-download bootstrap. It verifies Python 3.10+, then starts the guided installer. The wizard:

1. Detects Linux, WSL, SSH/RDP, containers, architecture, libc, and systemd-user availability locally.
2. Lets you choose **Headless** (recommended for cloud) or **Attach Desktop** (uses an already-running OmniRush desktop account).
3. Reuses a verified native sidecar or downloads the pinned official OmniRush 3.1.1 Linux runtime rootlessly.
4. Offers the native provider login in the terminal. Credentials are handled by the native CLI and are never captured by this project.
5. Guides BotFather `/newbot`, accepts the token with hidden input, validates `getMe`, refuses an existing webhook, and asks for your numeric Telegram user ID.
6. Selects a real project root/folder and reads the live provider/model catalog, including context limits.
7. Defaults to `ASK`; `FULL` requires typing `FULL` during setup and can later be enabled only with the exact Telegram phrase shown by `/mode`.
8. Offers a systemd **user** service or a truthful manual/supervisor path. Nothing is started or enabled without consent.

Cloud walkthroughs, free-tier caveats, EC2/VM commands, persistent service steps, and cleanup commands are in [`docs/cloud-guides.md`](docs/cloud-guides.md).

## First checks and lifecycle

After setup, run the read-only checks before starting the bridge:

```bash
python3 omnirush.py doctor
python3 omnirush.py status
python3 omnirush.py start
```

Useful lifecycle commands:

| Command | Purpose |
| --- | --- |
| `python3 omnirush.py doctor` | Read-only environment, Telegram, backend, and model checks |
| `python3 omnirush.py status` | Show systemd or verified supervisor state |
| `python3 omnirush.py start` | Start owned services or a verified detached supervisor |
| `python3 omnirush.py run` | Foreground supervisor for containers/SSH/diagnostics |
| `python3 omnirush.py stop` | Stop only this installation; preserves state |
| `python3 omnirush.py restart` | Explicit stop then start |
| `python3 omnirush.py login` | Run native provider login locally, interactively |
| `python3 omnirush.py service install --enable` | Install and optionally enable systemd user units |
| `python3 omnirush.py service uninstall` | Remove only units managed by this project |
| `python3 omnirush.py update --version 3.1.1` | Explicitly download a pinned runtime; never changes account/model |

For CI, automation, or terminals without color:

```bash
python3 omnirush.py --plain doctor
NO_COLOR=1 python3 omnirush.py --plain status
```

## Telegram experience

Open the bot from the owner account and press **Start**. Normal messages become queued OmniRush prompts in the selected thread. The bot sends a live, rate-limited progress card with elapsed time, then forwards only completed visible assistant text (not reasoning, raw tools, logs, or credentials).

Core commands:

| Command | Purpose |
| --- | --- |
| `/status` | Backend, project, thread, model mode, and active state |
| `/projects` / `/project /absolute/folder` | Inspect or select configured folders |
| `/threads` / `/thread ses_ID` | List/select bot-created conversations only |
| `/new [title]` | Start a new thread when the current one is idle |
| `/model` / `/model provider/id` | List live models or switch the current thread |
| `/usage` | Native session lifetime cost and token usage |
| `/context` | Native model context/output limits; not live occupancy |
| `/quota` | Honest availability notice; no quota numbers are fabricated |
| `/mode` | Show permission mode and exact opt-in instructions |
| `/pending` | Show native approvals/questions |
| `/approve per_ID` / `/deny per_ID` | Approve one action once or reject it |
| `/stop` | Interrupt active execution without replaying it |
| `/latest` | Explicitly resend the latest completed visible reply |
| `/get relative/file` | Send a checked non-hidden artifact up to 20 MiB |

Inline buttons are owner-bound, expire, and are tied to the message that created them. Guessed session IDs, group messages, forwarded messages, edited messages, bot messages, and callback replays are rejected.

## Deployment choices

### Cloud VM / dedicated Linux server

Choose **Headless**. Keep the VM, network, provider access, and Telegram connectivity available. If a working systemd user manager is available:

```bash
python3 omnirush.py service install --enable
python3 omnirush.py start
```

Optional `loginctl enable-linger "$USER"` lets the user manager survive logout; the wizard asks before requesting it and never uses `sudo`. Linger does not defeat provider outages, VM shutdown, host sleep, or cloud suspension. Prefer a provider with an explicit always-on VM policy.

### WSL

Choose **Headless** for a separate native runtime, or **Attach Desktop** only when the OmniRush desktop and its authenticated sidecar stay open. WSL itself stops when Windows shuts down or sleeps, so it is not a 24/7 guarantee. Use Windows power/startup policy separately if you need laptop continuity.

### Linux RDP / SSH

Both are supported. RDP is useful for Desktop attach; SSH is ideal for headless setup. Authentication is intentionally local-terminal-only. Browser website logins and desktop accessibility flows still require the appropriate desktop/browser experience.

### Containers

Mount a persistent volume for the private native auth/data directories and let the container platform restart the process. Run the real foreground supervisor:

```bash
python3 omnirush.py run
```

This repository does not create cron jobs, cloud schedules, firewall rules, public listeners, or Docker infrastructure. Container restart policy, secrets injection, networking, and persistent volumes remain deployment-owner responsibilities.

The pinned official runtime requires Linux x64/arm64 with glibc. Alpine/musl is rejected by the installer; use a glibc-based image or provide a verified compatible native sidecar.

## Private data and security model

Nothing sensitive belongs in this repository. Setup writes restrictive files outside the checkout:

```text
~/.config/omnirush-telegram-portable/config.json       mode 600
~/.local/state/omnirush-telegram-portable/state.sqlite3 mode 600
~/.local/share/omnirush-telegram-portable/native/       mode 700
~/.local/share/omnirush-telegram-portable/runtimes/     mode 700
```

The state database stores identifiers, offsets, policy metadata, and delivery claims—not the bot token, prompt text, titles, reasoning, raw tools, or assistant replies. The Telegram transport does not retry mutations after uncertain network results. Existing webhooks are never deleted automatically. Project selection is scoped to configured directories but is not an OS sandbox: an approved agent tool still runs with the Linux user’s existing privileges.

If a bot token leaks, revoke it with BotFather and rerun setup with a replacement. If you need hard isolation, use a dedicated restricted OS user or container; `FULL` mode does not provide that isolation.

## Architecture

```text
Telegram private chat
        │ long polling over HTTPS (owner-only)
        ▼
bot.py + telegram_bridge.bridge
        │ durable claims, project/thread scope, redacted visible replies
        ▼
AgentClient ── desktop mode ──> existing loopback OmniRush sidecar
            └─ headless mode ──> private native sidecar service
                                      │
                                      └─ provider auth/model/session APIs
```

`telegram_bridge.releases` verifies a pinned GitHub release asset and extracts only the expected native sidecar member. `telegram_bridge.services` installs only marked systemd user units or manages a verified application supervisor. `telegram_bridge.runtime` binds the native server to loopback and uses separate XDG paths for headless auth/data.

## Development

No third-party Python package is required. Run the full isolated test suite:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 -m compileall -q .
```

The suite covers owner authentication, callback replay protection, durable at-most-once claims, uncertain network outcomes, thread isolation, permission consent, progress throttling, model/context reporting, release checksum/archive safety, environment detection, service rendering, and private lifecycle state. Tests do not read live credentials, poll Telegram, download the 250 MB runtime, install services, or start a bot.

## Boundaries

- The native API exposes session cost, lifetime token usage, model context/output limits, and provider/model catalogs; it does not provide a verified account-quota endpoint used here, so `/quota` reports unavailable rather than inventing a number.
- Desktop/browser approvals, CAPTCHAs, sign-in pages, accessibility permissions, and cloud organization policy remain in their native UI flows.
- “24/7” means the host and its network are continuously available. This project cannot keep a laptop, WSL VM, cloud account, or provider alive by itself.
- Published as a public repository: [github.com/ShadowStudios1/omnirush-telegram](https://github.com/ShadowStudios1/omnirush-telegram).
