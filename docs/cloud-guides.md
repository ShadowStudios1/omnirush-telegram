# Cloud deployment guides ☁️

These are practical deployment paths for testing the portable bridge on an always-on Linux VM. They are not promises of free compute or free model usage. Cloud offers, eligibility, regions, billing plans, and AI-provider pricing change frequently; verify the current provider page before creating resources.

## First: “free tokens” clarified

There is no safe or legitimate source of unlimited free AWS, model-provider, or OmniRush API tokens. Never use a token found online, a shared credential, or multiple accounts to bypass a limit.

- **Telegram token:** create your own bot with `@BotFather`; it is not an AI token.
- **Cloud credits:** AWS, Google Cloud, and Azure may offer new-account credits or limited free services, subject to eligibility, expiry, region, and billing rules.
- **Model access:** OmniRush still needs a real authenticated model/provider account. A free VM does not make paid model inference free.
- **Cloud credentials:** the bridge does not ask for cloud access keys in Telegram. OmniRush account device login happens interactively in the VM terminal/browser and stores gateway credentials in private native paths.

As checked on **2026-10-05**, the official pages describe these broad offers:

| Provider | Useful test path | What “free” currently means | 24/7 warning |
| --- | --- | --- | --- |
| AWS | Ubuntu EC2 VM | AWS advertises up to **$200 credits for up to 6 months** on its Free plan; eligibility and selected services apply | Credits expire; enable billing alerts and do not assume EC2 is permanently free |
| Google Cloud | Compute Engine `e2-micro` | Google advertises **$300 new-customer credit** and a monthly free tier including one e2-micro, subject to location/eligibility limits | Use a supported US region and confirm the console’s free-tier estimate |
| Azure | Ubuntu VM | Azure advertises free monthly amounts for some services for 12 months plus always-free services | VM size/region eligibility and the account plan matter; some accounts must move to pay-as-you-go |
| Oracle Cloud | Always Free Ampere/AMD VM | Oracle advertises Always Free compute, but capacity and shape availability vary by region/account | “Always Free” still needs a valid account and available capacity; verify the current console offer |

Official references: [AWS Free Tier](https://aws.amazon.com/free/), [Google Cloud Free](https://cloud.google.com/free), [Azure Free Services](https://azure.microsoft.com/en-us/pricing/free-services/), and [Oracle Cloud Free](https://www.oracle.com/cloud/free/).

## Common VM preparation

The commands below assume a fresh Ubuntu 24.04 or similar glibc-based Linux VM and a non-root login user. The repository supports Linux x64 and arm64. The official pinned runtime is downloaded only after you approve it in setup.

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates python3
python3 --version       # must be 3.10 or newer
git clone https://github.com/ShadowStudios1/omnirush-telegram.git omnirush-telegram
cd omnirush-telegram
./setup.sh
```

During the wizard:

1. Select **Headless** for a cloud VM.
2. Approve the pinned official runtime download if no native sidecar is installed.
3. Approve the OmniRush account device-login link in your browser when offered. Do not paste provider credentials into Telegram or shell arguments. This is not OpenCode's generic `auth login` selector.
4. Create a narrow project root, such as `~/omnirush-projects`.
5. Select a live model from the API catalog.
6. Create a bot at `@BotFather` with `/newbot`, paste its token only into hidden setup input, and enter your numeric Telegram user ID.
7. Keep `ASK` for the first test. Use `FULL` only after understanding that the agent runs with the VM user’s existing privileges.
8. Choose the systemd user service when the wizard detects a working user manager.

If the OmniRush account login is not ready, stop and use the explicit local command later:

```bash
python3 omnirush.py login
python3 omnirush.py doctor
```

## AWS EC2: recommended test path

### 1. Create the instance in the AWS Console

1. Open **EC2 → Instances → Launch instance**.
2. Use Ubuntu Server 24.04 LTS, x86_64 for the broadest compatibility, or arm64/Graviton if your region’s eligible offer is available. The installer supports both.
3. Choose the smallest eligible instance shown by the current Free Tier/Free plan filter. Do not rely on an old blog post’s instance type.
4. Create or select an SSH key pair and download the private key once.
5. Create a security group with **SSH TCP/22 from your IP only**. Do not open the OmniRush backend port: Telegram uses outbound HTTPS long polling and the native service binds to loopback.
6. Leave outbound HTTPS enabled so the VM can reach Telegram, the native provider, and the official release source.
7. Add an EBS volume large enough for the runtime and your project, then launch.

The console is safer than copying a stale AMI ID or security-group ID into a script. Add an AWS Budget alert before testing so a free-tier boundary cannot become a surprise bill.

### 2. SSH and install

Replace placeholders locally; do not paste private keys into chat.

```bash
chmod 400 <PATH_TO_KEY.pem>
ssh -i <PATH_TO_KEY.pem> ubuntu@<EC2_PUBLIC_IP>

sudo apt-get update
sudo apt-get install -y git ca-certificates python3
git clone https://github.com/ShadowStudios1/omnirush-telegram.git omnirush-telegram
cd omnirush-telegram
./setup.sh
```

### 3. Make the application survive SSH logout

The guided setup can install the systemd user units. On a VM, explicitly request user-manager persistence if your account policy permits it:

```bash
python3 omnirush.py doctor
python3 omnirush.py service install --enable --linger
python3 omnirush.py start
python3 omnirush.py status
```

The command asks for confirmation. It never uses `sudo`, creates a public listener, or changes the firewall. If `loginctl enable-linger` is denied, ask the account administrator or keep the process under the VM provider’s supported supervisor; do not rerun the installer as root.

Verify after closing and reopening SSH:

```bash
ssh -i <PATH_TO_KEY.pem> ubuntu@<EC2_PUBLIC_IP>
cd ~/omnirush-telegram
python3 omnirush.py status
systemctl --user is-active omnirush-telegram-portable.service
```

If systemd user services are not available, use the honest fallback:

```bash
cd ~/omnirush-telegram
python3 omnirush.py run
```

That foreground supervisor is appropriate for a container or an external VM supervisor. `python3 omnirush.py start` can detach a verified process, but it is not a reboot guarantee when no user manager is available.

### 4. Update, inspect, and clean up

```bash
cd ~/omnirush-telegram
python3 omnirush.py doctor
python3 omnirush.py status
python3 omnirush.py update --version 3.1.1
```

Do not delete the private native data directory while troubleshooting; it contains the headless provider authentication and runtime state. To stop only this application while preserving all data:

```bash
python3 omnirush.py stop
```

When finished testing, stop the bridge and **stop or terminate the EC2 instance in the AWS Console**. Terminating is destructive to the VM’s ephemeral state; preserve an image/backup first if needed. Free-tier eligibility does not cover every EBS, public IPv4, snapshot, data-transfer, or AI-provider charge.

## Google Cloud Compute Engine

Google’s current free page advertises $300 in new-customer credit and a free tier including one e2-micro per month, subject to location and eligibility limits.

1. Open **Compute Engine → VM instances → Create instance**.
2. Select Ubuntu 24.04, a supported region/zone listed by the current free-tier documentation, and the smallest eligible e2-micro shape.
3. Allow SSH only from your IP; no application port is required.
4. Use the browser SSH button or local `gcloud compute ssh`, then follow the [Common VM preparation](#common-vm-preparation) commands.
5. Run `python3 omnirush.py service install --enable --linger`, `start`, and `status` as shown in the AWS section.

Google free-tier limits are region-specific and can change. Confirm the estimate in the console before pressing **Create**.

## Microsoft Azure Linux VM

Azure’s current free-services page describes selected 12-month free amounts for new customers and separate always-free monthly services. VM size and region eligibility must be checked in the portal.

1. Open **Virtual machines → Create** and choose Ubuntu 24.04 LTS.
2. Select a VM size explicitly marked eligible by the current Azure offer for your account.
3. Restrict SSH to your IP in the networking page; do not add an OmniRush inbound rule.
4. Connect with the portal SSH option or `az vm ssh`, then run the common preparation and setup commands.
5. Install the systemd user services and verify with `python3 omnirush.py status`.

Azure’s free-service terms may require moving to pay-as-you-go after the initial period to continue using the account. Set a budget alert and remove the VM when testing ends.

## Oracle Cloud Free Tier

Oracle advertises Always Free compute shapes, including ARM options, but new-account verification, region capacity, and shape availability are real constraints.

1. Create an Oracle Cloud account and choose a home region carefully; some resources are region-bound.
2. Create an Ubuntu arm64 Ampere VM only if the console marks the selected shape as Always Free and capacity is available. The bridge supports arm64.
3. Add an SSH key and allow only SSH from your IP. No inbound Telegram/OmniRush port is needed.
4. SSH into the VM and run the common preparation and setup commands.
5. Use `python3 omnirush.py service install --enable --linger`, `start`, and `status`.

If the ARM shape is unavailable, try another eligible region from the console rather than running repeated automation against the API. Always Free does not mean every attached disk, public IP, bandwidth tier, or provider/model call is free.

## Provider/model access on a cloud VM

The bridge’s cloud role is compute and secure transport; it does not provide model credits. Choose one supported model/provider in the native login flow:

- OmniRush-managed access, if your account and native installation offer it;
- a provider account/API key entered through the native CLI login flow;
- a provider’s own trial/credit offer, after reading its terms.

Keep provider credentials in the private native XDG data directory created by headless mode. Never put them in project files, Git, Telegram messages, unit files, or `ps` command arguments. `/quota` intentionally reports unavailable when the native API has no verified account-quota endpoint.

## Cloud troubleshooting

```bash
cd ~/omnirush-telegram
python3 omnirush.py doctor
python3 omnirush.py status
systemctl --user status omnirush-telegram-portable.service --no-pager
systemctl --user status omnirush-telegram-portable-backend.service --no-pager
```

- **No model:** run `python3 omnirush.py login` locally on the VM, then `doctor`.
- **Telegram 409:** another poller owns the bot. Stop the old bridge; this project never deletes a webhook or takes over silently.
- **Cloud SSH works but bot stops after logout:** inspect systemd user manager/linger; use `run` under the provider’s process supervisor.
- **WSL or laptop stops:** host sleep/shutdown is outside this project. Use an always-on VM for 24/7 testing.
- **Runtime rejected:** the pinned installer requires Linux x64/arm64 with glibc; use Ubuntu/Debian/RHEL-like images, not Alpine/musl.
