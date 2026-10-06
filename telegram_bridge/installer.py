"""Consent-driven portable setup. Accounts, secrets and models are never guessed."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
from types import SimpleNamespace

from .cli_ui import UI, require_terminal, safe_text
from .config import CONFIG_PATH, STATE_PATH, ConfigError, load_config, save_private
from .environment import detect_environment, refuse_root
from . import services


PINNED_VERSION = "3.1.1"


class SetupError(RuntimeError):
    pass


def install_official_cli(ui):
    """Install or verify the official OmniRush CLI and private launcher."""
    from .cli_runtime import CliRuntimeError, ensure_official_cli, install_launcher, launcher_path_in_path
    try:
        with ui.busy("Downloading and verifying official OmniRush CLI"):
            runtime = ensure_official_cli()
        install_launcher(runtime)
    except CliRuntimeError as error:
        raise SetupError(str(error)) from None
    if not launcher_path_in_path():
        ui.say("Official `omnirush` is installed at ~/.local/bin/omnirush, but that folder is not in this shell's PATH.", "warn")
        ui.say('Run `export PATH="$HOME/.local/bin:$PATH"` for this shell, or open a new login shell.', "warn")
    else:
        ui.say("Official `omnirush` command installed and ready.", "ok")
    return runtime


def login(executable, backend_mode, ui):
    require_terminal()
    if backend_mode == "desktop":
        ui.say("Desktop account login is handled by the OmniRush GUI: open Settings > Account and sign in there.", "warn")
        ui.say("Keep the desktop app open; this installer does not sign into or import the desktop account.")
        return False
    ui.say("OmniRush account login runs in this terminal only; never paste credentials into Telegram.", "warn")
    if not ui.confirm("Start OmniRush account login now?"):
        return False
    from .account import AccountError, login as account_login
    try:
        account_login(ui)
    except AccountError as error:
        raise SetupError(str(error)) from None
    ui.say("OmniRush account login completed; native credentials saved privately outside the project.", "ok")
    return True


def select_executable(ui, existing=None, backend_mode=None):
    if backend_mode == "headless":
        from .cli_runtime import CliRuntimeError
        try:
            with ui.busy("Downloading and verifying official OmniRush CLI"):
                runtime = ensure_official_cli()
            install_launcher(runtime)
        except CliRuntimeError as error:
            raise SetupError(str(error)) from None
        if not launcher_path_in_path():
            ui.say("Official `omnirush` was installed at ~/.local/bin/omnirush, but ~/.local/bin is not in this shell's PATH.", "warn")
            ui.say("Add `export PATH=\"$HOME/.local/bin:$PATH\"` to your shell profile yourself, then open a new shell. No shell files were edited.")
        else:
            ui.say("Official CLI launcher installed at ~/.local/bin/omnirush.", "ok")
        return str(runtime.engine)
    from .agent import discover_executable
    found = existing.executable if existing and Path(existing.executable).is_file() and os.access(existing.executable, os.X_OK) else discover_executable()
    if found:
        ui.say("Installed sidecar: " + safe_text(found))
        if ui.confirm("Reuse this executable (no download)?", default=True):
            return str(Path(found).absolute())
    ui.say(f"A verified official OmniRush {PINNED_VERSION} sidecar can be installed rootlessly in your private user data directory.")
    if not ui.confirm("Download and verify the pinned runtime now?"):
        raise SetupError("No executable selected. Nothing was downloaded; install a supported OmniRush sidecar and retry.")
    from .releases import ensure_runtime
    with ui.busy("Downloading and verifying pinned official runtime"):
        return str(ensure_runtime(version=PINNED_VERSION))


def select_project(ui, existing=None):
    default = str(existing.roots[0]) if existing else str(Path.home() / "omnirush-projects")
    root = Path(ui.prompt("Allowed project root (not / or your entire home)", default)).expanduser().resolve()
    if root == Path("/") or any(path.resolve().is_relative_to(root) for path in (CONFIG_PATH, STATE_PATH)):
        raise SetupError("Project roots must not expose private configuration/auth/state. Use a dedicated project folder.")
    # Headless runtime authentication is also under private application paths.
    for private in (Path.home() / ".config/omnirush-telegram-portable",
                    Path.home() / ".local/share/omnirush-telegram-portable",
                    Path.home() / ".local/state/omnirush-telegram-portable"):
        if private.resolve().is_relative_to(root):
            raise SetupError("That project root includes private bridge/runtime files; choose a narrower root.")
    if not root.exists():
        if not ui.confirm("Create project root " + str(root) + "?"):
            raise SetupError("Project folder was not created; setup cancelled.")
        root.mkdir(mode=0o700, parents=True)
    if not root.is_dir():
        raise SetupError("The project root must be an existing directory.")
    folders = [root]
    try:
        folders.extend(sorted((p.resolve() for p in root.iterdir()
                               if p.is_dir() and p.resolve().is_relative_to(root)), key=str)[:100])
    except OSError:
        raise SetupError("Cannot list project root; check its local permissions.") from None
    project = folders[ui.choose("Default project folder", [str(p) for p in folders])]
    if existing and root == existing.roots[0] and project == root and existing.project.is_relative_to(root):
        if ui.confirm("Keep existing default project " + str(existing.project) + "?", default=True):
            project = existing.project
    return (root,), project


def permission_choice(ui, existing=None):
    ui.say("ASK is the safe default: approve supported permission requests from your private Telegram chat.")
    ui.say("FULL allows agent tools without permission prompts for bridge-owned sessions. It is NOT an OS sandbox: the process can access everything your Linux user can. It does not bypass root, organization or website policy.", "warn")
    default = 1 if existing and existing.permission_mode == "full" else 0
    selected = ui.choose("Permission mode", ["ASK — permission prompts", "FULL — unrestricted agent tools within your user privileges"], default=default)
    if selected == 0:
        return "ask"
    if ui.prompt("Type FULL to consent explicitly") != "FULL":
        raise SetupError("FULL consent not given. Configuration was not saved.")
    return "full"


def model_entries(payload):
    """Normalize the native provider catalog, not a hard-coded model menu."""
    entries = []
    # Verified native /api/model contract: {location, data: Model.Info[]}.
    # Agent adapters may already unwrap the envelope to a flat model list.
    if isinstance(payload, dict) and "data" in payload:
        payload = payload["data"]
    if isinstance(payload, list) and all(isinstance(model, dict) and "providerID" in model for model in payload):
        for model in payload:
            if (not isinstance(model.get("id"), str) or not isinstance(model.get("providerID"), str)
                    or model.get("enabled") is False):
                continue
            selected = {"providerID": model["providerID"], "id": model["id"]}
            limit = model.get("limit", {})
            context = limit.get("context") if isinstance(limit, dict) else None
            label = f"{selected['providerID']}/{selected['id']} — {model.get('name', model['id'])}; context: " + (str(context) if type(context) is int else "not reported")
            entries.append((selected, safe_text(label)))
        return sorted(entries, key=lambda pair: (pair[0]["providerID"], pair[0]["id"]))
    providers = payload.get("providers", []) if isinstance(payload, dict) else payload
    if isinstance(providers, dict):
        providers = list(providers.values())
    if not isinstance(providers, list):
        raise SetupError("Agent model catalog was not understood; no model was selected.")
    for provider in providers:
        if not isinstance(provider, dict) or not isinstance(provider.get("id"), str):
            continue
        models = provider.get("models", {})
        if isinstance(models, dict):
            models = list(models.values())
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                continue
            selected = {"providerID": provider["id"], "id": model["id"]}
            limit = model.get("limit", {})
            context = limit.get("context") if isinstance(limit, dict) else None
            name = model.get("name", model["id"])
            label = f"{provider['id']}/{model['id']} — {name}; context: " + (str(context) if type(context) is int else "not reported")
            entries.append((selected, safe_text(label)))
    return sorted(entries, key=lambda pair: (pair[0]["providerID"], pair[0]["id"]))


def select_model(ui, client, directory, existing=None):
    with ui.busy("Reading actual agent provider/model catalog"):
        entries = model_entries(client.models(directory=str(directory)))
        if not entries and getattr(client, "backend_mode", None) == "headless":
            # The official CLI's account catalog is authoritative. This
            # fallback also keeps setup useful if an engine starts before its
            # generated config has been observed by /api/model.
            from .account import AccountError, model_catalog
            try:
                catalog = model_catalog()
            except AccountError:
                catalog = []
            entries = model_entries({
                "data": [{"id": item["id"], "providerID": "omnirush",
                           "name": item.get("display_name", item["id"]),
                           "limit": item.get("limits", {})}
                          for item in catalog]
            })
        if not entries:
            if getattr(client, "backend_mode", None) == "headless":
                raise SetupError("OmniRush account is signed in, but this gateway reported no models. Check account model entitlement or gateway access.")
            raise SetupError("The desktop reported no models. Sign in through OmniRush Settings > Account and check model access.")
        native_default = client.default_model(directory=str(directory))
        if isinstance(native_default, dict) and "data" in native_default:
            native_default = native_default["data"]
    if existing and existing.model is not None:
        ui.say(f"Existing model: {existing.model.get('providerID', '?')}/{existing.model.get('id', '?')}")
        if ui.confirm("Keep the existing explicit model (no account/model change)?", default=True):
            if not any(m["id"] == existing.model.get("id") and m["providerID"] == existing.model.get("providerID") for m, _ in entries):
                ui.say("Existing model is not in the current API catalog; verify provider access before starting.", "warn")
            return dict(existing.model)
    default_label = "Native backend default (does not change account or model)"
    if isinstance(native_default, dict):
        default_label += f" — {native_default.get('providerID', '?')}/{native_default.get('id', '?')}"
    choices = ([default_label] if native_default is not None else []) + [label for _, label in entries]
    selected = ui.choose("Model selection — provider IDs and context limits from API", choices)
    if native_default is not None:
        return None if selected == 0 else entries[selected - 1][0]
    return entries[selected][0]


def telegram_identity(ui, existing=None):
    from .telegram import TelegramClient
    ui.title("Telegram / BotFather")
    ui.say("Open the verified @BotFather account in Telegram, send /newbot and follow its instructions.")
    ui.say("Treat the token like a password. If it leaks, send /revoke to @BotFather and replace it here.", "warn")
    if existing and ui.confirm("Reuse the stored hidden bot token?", default=True):
        token = existing.token
    else:
        token = ui.secret("Paste BotFather bot token (hidden)").strip()
    with ui.busy("Validating Telegram getMe and webhook (read-only)"):
        telegram = TelegramClient(token)
        identity = telegram.get_me()
        webhook = telegram.webhook_info()
    if not isinstance(identity, dict) or identity.get("is_bot") is not True:
        raise SetupError("Telegram did not return a valid bot identity.")
    if not isinstance(webhook, dict) or webhook.get("url"):
        raise SetupError("Existing webhook found or webhook state unknown. It was NOT deleted. Resolve polling ownership explicitly before setup.")
    ui.say("Validated bot: @" + safe_text(identity.get("username", "unknown")), "ok")
    ui.say("Owner ID: your numeric Telegram user ID, NOT your @username or the bot ID. Use a Telegram client that displays IDs, or @userinfobot (third-party; send it no bot token).")
    while True:
        value = ui.prompt("Owner numeric user ID", str(existing.owner_id) if existing else None)
        if value.isascii() and value.isdigit() and 0 < int(value) < 2**63:
            return token, int(value)
        ui.say("Enter a positive numeric user ID.")


def setup(ui=None):
    ui = ui or UI()
    refuse_root()
    require_terminal()
    ui.title("OmniRush Telegram Portable — guided setup")
    info = detect_environment()
    for line in info.lines():
        ui.say(line)
    ui.say("No cloud metadata probing, sudo, public listeners or firewall changes. Credentials stay in private user paths.")
    existing = None
    if CONFIG_PATH.exists() or CONFIG_PATH.is_symlink():
        existing = load_config()
        if services.is_running(existing):
            raise SetupError("An existing bot is running or initializing. Stop it explicitly before changing configuration; nothing was changed.")
        ui.say("Existing private configuration found. No token/account/model will be changed silently.")
        if not ui.confirm("Review and replace existing configuration? (No keeps it unchanged)"):
            ui.say("Existing configuration preserved.", "ok")
            return 0
    backend = ["headless", "desktop"][ui.choose(
        "Backend", ["Headless — private native server; desktop app not required", "Attach Desktop — use existing desktop account; app must remain open"],
        default=1 if existing and existing.backend_mode == "desktop" else 0)]
    if backend == "desktop":
        ui.say("Desktop attach cannot run independently of a logged-in, open desktop app. No account import or model switch is performed.", "warn")
        ui.say("Sign into OmniRush GUI Settings > Account before continuing; desktop credentials are never imported.")
    elif existing and existing.backend_mode != "headless":
        ui.say("Headless auth is separate from Desktop. Desktop credentials are not copied.", "warn")
    executable = select_executable(ui, existing, backend)
    if backend == "headless":
        ui.title("OmniRush account login")
        login(executable, backend, ui)
    roots, project = select_project(ui, existing)
    permission = permission_choice(ui, existing)
    temporary_config = SimpleNamespace(executable=executable, server_url="managed" if backend == "headless" else "auto",
                                       model=None, agent=None, permission_mode=permission, backend_mode=backend)
    @contextmanager
    def desktop():
        client = services._client(temporary_config)
        client.health()
        yield client
    with (services.backend_process(temporary_config) if backend == "headless" else desktop()) as client:
        model = select_model(ui, client, project, existing)
    token, owner = telegram_identity(ui, existing)
    data = {
        "telegram_token": token, "owner_id": owner,
        "default_project": str(project), "project_roots": [str(root) for root in roots],
        "executable": executable, "server_url": temporary_config.server_url,
        "backend_mode": backend, "permission_mode": permission, "model": model,
        "agent": existing.agent if existing else None,
        "state_path": str(existing.state_path if existing else STATE_PATH),
        "monitor_interval": existing.monitor_interval if existing else 5,
    }
    # Last-moment guard: another terminal may have started the old bot while the
    # wizard was open. A running configuration is never silently replaced.
    if existing and services.is_running(existing):
        raise SetupError("The existing bot started during setup. Stop it explicitly; configuration was not saved.")
    ui.title("Confirm private configuration")
    ui.say(f"Backend: {backend}; permission: {permission}; project: {project}; owner ID: {owner}")
    ui.say("Model: " + (f"{model['providerID']}/{model['id']}" if model else "native backend default"))
    ui.say("Bot token: hidden. Config: " + str(CONFIG_PATH))
    if not ui.confirm("Save this private configuration?"):
        ui.say("Configuration was not saved. Any explicitly created project/download/login remains available.")
        return 0
    save_private(data)
    config = load_config()
    ui.say("Private configuration saved. No live bot has been started yet.", "ok")
    if backend == "headless":
        ui.say("The official OmniRush CLI/native backend is installed privately; it will run autonomously only after explicit service/start consent.")
    if info.systemd_user:
        selected = ui.choose("Service lifecycle", ["Keep manual/foreground control", "Install systemd USER application services"])
        if selected == 1:
            persist = ui.confirm("Enable these application services on future user-manager starts (persistence)?")
            services.install(config, enable=persist)
            if persist:
                ui.say("By default a user manager may stop on logout. Linger keeps it available after logout and at boot; it does not defeat sleep/offline.", "warn")
                if ui.confirm("Request loginctl enable-linger for this user? (No sudo; may require administrator approval)"):
                    services.enable_linger()
    else:
        ui.say("No working systemd user manager. `python3 omnirush.py run` is a real foreground/container supervisor. `start` is terminal-detached; logout policies may stop it and reboot persistence is NOT provided.", "warn")
    if ui.confirm("Start the configured bot now?"):
        ui.say(services.start(config), "ok")
    else:
        ui.say("Ready. Start explicitly with `python3 omnirush.py start`; inspect with `doctor` and `status`.", "ok")
    return 0


def doctor(ui):
    refuse_root()
    for line in detect_environment().lines():
        ui.say(line)
    config = load_config()
    ui.say(f"Private config: valid; backend: {config.backend_mode}; permission: {config.permission_mode}", "ok")
    ui.say(services.status(config))
    from .telegram import TelegramClient
    with ui.busy("Read-only Telegram identity/webhook checks"):
        telegram = TelegramClient(config.token)
        identity = telegram.get_me()
        webhook = telegram.webhook_info()
    if not isinstance(webhook, dict) or webhook.get("url"):
        raise SetupError("Webhook prevents polling or is unknown; no webhook was changed.")
    ui.say("Telegram identity: @" + safe_text(identity.get("username", "unknown")), "ok")
    # Doctor never starts a missing backend, authenticates or repairs silently.
    with ui.busy("Read-only local backend check"):
        client = services._client(config)
        client.health()
        models = model_entries(client.models(directory=str(config.project)))
    ui.say(f"Backend reachable; {len(models)} API model entries. No changes made.", "ok")
    return 0
