#!/usr/bin/env python3
"""Private owner-only Telegram bridge setup and application lifecycle."""
from __future__ import annotations

import argparse
import sys


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Don't echo unknown arguments: they might contain an accidental token.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid command. Use --help; credentials are never accepted as arguments.\n")


def parser():
    result = Parser(description=__doc__, allow_abbrev=False)
    result.add_argument("--plain", action="store_true", help="Disable colors and animation (also honors NO_COLOR/non-TTY)")
    sub = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    descriptions = {
        "setup": "Guided terminal setup; no account/model changes without consent",
        "doctor": "Read-only environment, private config, Telegram and backend checks",
        "status": "Inspect systemd or verified supervisor state",
        "start": "Start user service or detach a supervised bot (no fallback reboot durability)",
        "stop": "Stop only owned services or a verified supervisor; preserve state",
        "restart": "Explicit stop then start; never reset operational state",
        "login": "OmniRush account device login for the private native runtime",
        "run": "Foreground supervisor for terminals/containers; handles SIGTERM/Ctrl-C",
    }
    for name, description in descriptions.items():
        child = sub.add_parser(name, help=description, description=description, allow_abbrev=False)
        child.add_argument("--plain", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    service = sub.add_parser("service", help="Install/uninstall consented systemd USER application services", allow_abbrev=False)
    actions = service.add_subparsers(dest="action", required=True, parser_class=Parser)
    install = actions.add_parser("install", allow_abbrev=False)
    install.add_argument("--enable", action="store_true", help="Explicitly enable future user-manager startup (does not start now)")
    install.add_argument("--linger", action="store_true", help="Ask for linger consent; never sudo")
    actions.add_parser("uninstall", allow_abbrev=False)
    update = sub.add_parser("update", help="Explicit verified runtime version download; does not switch accounts or start bot", allow_abbrev=False)
    update.add_argument("--version", required=True, help="Exact official runtime version; no implicit latest")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if sys.version_info < (3, 10):
        print("Python 3.10+ required; use ./setup.sh for distribution install guidance.", file=sys.stderr)
        return 1
    from telegram_bridge.cli_ui import UI, require_terminal
    from telegram_bridge.environment import refuse_root
    from telegram_bridge.config import load_config
    from telegram_bridge import installer, services
    ui = UI(plain=args.plain)
    try:
        refuse_root()
        if args.command == "setup":
            return installer.setup(ui)
        if args.command == "doctor":
            return installer.doctor(ui)
        if args.command == "update":
            require_terminal()
            from telegram_bridge.releases import ensure_runtime
            ui.say("This downloads a verified version only. It does NOT silently change the configured executable/model/account.")
            if not ui.confirm("Download exact official runtime " + args.version + "?"):
                return 0
            with ui.busy("Downloading and verifying official runtime"):
                path = ensure_runtime(version=args.version)
            ui.say("Verified runtime: " + str(path) + ". Run setup explicitly to select it.", "ok")
            return 0
        if args.command == "login":
            from telegram_bridge.config import CONFIG_PATH
            require_terminal()
            if CONFIG_PATH.exists() or CONFIG_PATH.is_symlink():
                config = load_config()
                if services.is_running(config):
                    raise installer.SetupError("Stop the running bot before changing its OmniRush account authentication.")
                installer.login(config.executable, config.backend_mode, ui)
            else:
                installer.login(None, "headless", ui)
            return 0
        config = load_config()
        if args.command == "status":
            ui.say(services.status(config))
        elif args.command == "start":
            ui.say(services.start(config))
        elif args.command == "stop":
            ui.say(services.stop(config))
        elif args.command == "restart":
            ui.say(services.stop(config))
            ui.say(services.start(config))
        elif args.command == "run":
            return services.run(config)
        elif args.command == "service":
            require_terminal()
            if args.action == "uninstall":
                if ui.confirm("Stop and uninstall only this application's user services? Config/auth/state are preserved."):
                    services.uninstall()
                    ui.say("Owned user service units removed; private data preserved.")
            else:
                if services.is_running(config):
                    raise installer.SetupError("Stop the existing bot before installing/replacing its services.")
                if ui.confirm("Install systemd USER backend/bot services (no sudo)?"):
                    services.install(config, enable=args.enable)
                    if args.linger:
                        ui.say("Linger keeps your user manager available after logout and at boot. It cannot prevent VM/host sleep or offline downtime.", "warn")
                        if ui.confirm("Request loginctl enable-linger for this user, without sudo?"):
                            services.enable_linger()
                    ui.say("User services installed. Start separately with `start`.", "ok")
        return 0
    except (KeyboardInterrupt, EOFError):
        ui.say("Cancelled. No automatic retry; private configuration/state preserved. Inspect status if startup was interrupted.", "warn")
        return 130
    except Exception:
        # Only our reviewed safe errors may be printed, never raw payloads,
        # URLs, traceback, native diagnostics or unexpected exception text.
        from telegram_bridge.config import ConfigError
        from telegram_bridge.cli_ui import TerminalError
        from telegram_bridge.environment import EnvironmentError
        from telegram_bridge.agent import AgentError
        from telegram_bridge.telegram import TelegramError
        from telegram_bridge.state import StateError
        from telegram_bridge.account import AccountError
        error = sys.exc_info()[1]
        safe_errors = (ConfigError, AgentError, TelegramError, StateError,
                       installer.SetupError, services.ServiceError, TerminalError, EnvironmentError, AccountError)
        if isinstance(error, safe_errors):
            ui.say(str(error), "error")
        elif type(error) is RuntimeError:
            # These two runtime checks contain fixed, non-secret messages.
            ui.say("Operation unavailable. Use an unprivileged Linux user and an interactive terminal for setup/login.", "error")
        else:
            ui.say("Operation failed safely; no automatic retry. Check local permissions, configuration and status. Private state was preserved.", "error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
