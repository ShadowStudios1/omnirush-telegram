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

## AWS EC2 setup, step by step

A complete, beginner-friendly walkthrough for running the bridge on a small always-on AWS EC2 Ubuntu VM. If you have never used a cloud server before, follow it top to bottom — every command is copy-paste, one at a time. Budget about 20 minutes.

> **Cost note:** a Free Tier–eligible micro instance is enough to test. A free VM does **not** make model/provider usage free, and Free Tier limits change — check the AWS console and set a budget alert before you start.

### What you need first

- An AWS account (with a payment method on file).
- Telegram installed on your phone or desktop.
- An OmniRush provider/model account — you will sign in during setup.

### Part 1 — Create the EC2 instance (in the AWS website)

1. Sign in to the **AWS Management Console**.
2. In the top-right corner pick a **region** close to you (for example `us-east-1`). Everything below happens in this region.
3. In the search bar type **EC2** and open the **EC2** service.
4. Click the orange **Launch instance** button.
5. **Name and tags** → Name: `omnirush-telegram`.
6. **Application and OS Images (Amazon Machine Image)** → choose **Ubuntu**, then **Ubuntu Server 24.04 LTS (HVM), SSD Volume Type**. Leave **Architecture** as **64-bit (x86)**. (arm64 is also supported.)
7. **Instance type** → choose **t3.micro** (or **t2.micro**). These are usually Free Tier eligible. Do not pick a larger type unless you accept the cost.
8. **Key pair (login)** → click **Create new key pair**.
   - Key pair name: `omnirush-key`
   - Key pair type: **RSA**; private key file format: **.pem**
   - Click **Create key pair**. The file downloads **once** — you can never download it again. Treat it like a password: never share it, commit it, or paste it into a chat.
9. **Network settings** → click **Edit**:
   - **Allow SSH traffic from** → choose **My IP** (not "Anywhere").
   - Leave everything else at the default. Do **not** open HTTP/HTTPS or any OmniRush port — the bridge only makes *outbound* connections, so no inbound port is needed.
10. **Configure storage** → **20 GiB gp3** is a comfortable size for the runtime and your project.
11. **Advanced details** → leave the defaults.
12. Click **Launch instance**, then **View all instances**.
13. Wait until **Instance state** is **Running** and **Status check** shows **2/2 checks passed**.
14. Select the instance and copy its **Public IPv4 address** (looks like `18.234.5.6`). You will paste it into every SSH command below.

### Part 2 — Connect to the instance from your computer

Choose the block for your operating system. Replace `<PUBLIC_IP>` with the address from step 14.

**Windows (PowerShell):**

```powershell
# Lock down the key file so Windows SSH accepts it (run once)
icacls "$env:USERPROFILE\Downloads\omnirush-key.pem" /inheritance:r
icacls "$env:USERPROFILE\Downloads\omnirush-key.pem" /grant:r "$env:USERNAME:R"

# Connect
ssh -i "$env:USERPROFILE\Downloads\omnirush-key.pem" ubuntu@<PUBLIC_IP>
```

**macOS / Linux:**

```bash
chmod 400 ~/Downloads/omnirush-key.pem
ssh -i ~/Downloads/omnirush-key.pem ubuntu@<PUBLIC_IP>
```

The first connection asks `Are you sure you want to continue connecting?` — type `yes`.

Success looks like a prompt ending in `ubuntu@ip-172-31-…:~$`. **From this point on, every command runs on the EC2 instance, not on your own computer.**

### Part 3 — Install the project (one line at a time)

1. Refresh the package list:

```bash
sudo apt-get update
```

2. Install Git and Python (Ubuntu 24.04 already ships Python 3.12):

```bash
sudo apt-get install -y git ca-certificates python3
```

3. Confirm Python is 3.10 or newer — it must print `3.10` or higher:

```bash
python3 --version
```

4. Download the project (it is public, so no login is needed):

```bash
git clone https://github.com/ShadowStudios1/omnirush-telegram.git
```

5. Enter the folder:

```bash
cd omnirush-telegram
```

6. Run the guided setup:

```bash
./setup.sh
```

> If `./setup.sh` reports `Permission denied`, run `chmod +x setup.sh` and retry — or simply `bash setup.sh`.

### Part 4 — Answer the setup wizard

`./setup.sh` starts a guided, animated wizard in this terminal. Answer each prompt:

| Prompt | What to choose |
| --- | --- |
| Consent prompts | Read them; the wizard never uses `sudo` or starts anything without asking |
| Backend mode | **Headless** — the right choice for a cloud VM |
| Native runtime | Approve the pinned official runtime download (~250 MB); it verifies the checksum |
| Provider login | Run the native provider login in this terminal when offered. Credentials go to the native CLI, **never** to this project or to Telegram |
| Project root | Create/select a folder, e.g. `/home/ubuntu/omnirush-projects` |
| Model | Pick a model from the live catalog the wizard lists |
| Bot token | See *BotFather* below |
| Owner ID | See *Your numeric ID* below |
| Permission mode | Keep **ASK** for your first run |
| systemd user service | Answer **yes** to install it |

**BotFather — create the bot:**

1. In Telegram, open a chat with `@BotFather`.
2. Send `/newbot`.
3. Give it a display name (anything), then a username **ending in `bot`** (for example `my_omnirush_bot`).
4. BotFather replies with a token like `123456789:AA…`. Copy it and paste it into the wizard — the input is hidden and the token is stored outside the repository.

**Your numeric ID — the one account allowed to use the bot:**

- This is your personal Telegram **user ID** (a number), **not** your `@username` and **not** the bot's ID.
- Easiest route: message `@userinfobot` and copy the number it returns. It is a third-party bot — never send it your bot token.

### Part 5 — Start it and verify

Run these one at a time:

```bash
python3 omnirush.py doctor
```

```bash
python3 omnirush.py service install --enable --linger
```

```bash
python3 omnirush.py start
```

```bash
python3 omnirush.py status
```

`--linger` keeps your user services running after you close the SSH window (it never uses `sudo`). Confirm the service is live:

```bash
systemctl --user is-active omnirush-telegram-portable.service
```

It should print `active`.

### Part 6 — Use it from Telegram

1. Open Telegram and find your bot (the `@username` you created).
2. Press **Start**.
3. Send any normal message — it becomes a prompt for your OmniRush agent, and the completed reply comes back in the chat.
4. Try `/status` to see the backend, project, thread, and mode.

### Part 7 — Come back later

Reconnect to the VM at any time:

```bash
ssh -i ~/Downloads/omnirush-key.pem ubuntu@<PUBLIC_IP>
```

```bash
cd omnirush-telegram && python3 omnirush.py status
```

- The **Public IPv4 address changes** if you stop and start the instance — update `<PUBLIC_IP>` accordingly. An **Elastic IP** keeps a fixed address.
- To intentionally restart the bridge: `python3 omnirush.py restart`.

### Part 8 — Stop and clean up (avoid surprise bills)

Stop the bridge:

```bash
python3 omnirush.py stop
```

Then in **AWS Console → EC2 → Instances**, select the instance and use **Instance state**:

- **Stop** — keeps the disk, stops compute charges.
- **Terminate** — deletes the instance and its disk.

Set a budget alert under **Billing → Budgets** so a Free Tier boundary can never become a surprise bill.

### If something goes wrong

| Symptom | Fix |
| --- | --- |
| `Permission denied (publickey)` | Wrong key or username — the Ubuntu user is `ubuntu` |
| `UNPROTECTED PRIVATE KEY FILE` (Windows) | Run the two `icacls` commands in Part 2 |
| `./setup.sh: Permission denied` | Run `chmod +x setup.sh` and retry, or `bash setup.sh` |
| Telegram error `409` | Another process is polling the same bot — stop the old bridge; this project never deletes a webhook silently |
| Bot stops after you close SSH | Confirm `--linger` was used and `systemctl --user is-active …` says `active` |
| `No model` | Run `python3 omnirush.py login` on the VM, then `doctor` |
| Runtime rejected | The pinned runtime needs Linux x64/arm64 with glibc — Ubuntu 24.04 is fine, Alpine/musl is not |

For Google Cloud, Azure, Oracle, WSL, and the container paths, see [`docs/cloud-guides.md`](docs/cloud-guides.md).

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

> **New to cloud servers?** The [AWS EC2 setup, step by step](#aws-ec2-setup-step-by-step) walkthrough above covers creating an instance and running these commands one at a time.

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
