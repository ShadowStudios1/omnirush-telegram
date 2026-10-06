#!/usr/bin/env python3
"""Owner-only bot entry point. No command-line credential arguments accepted."""

import argparse
import signal

from telegram_bridge.agent import AgentClient, AgentError
from telegram_bridge.bridge import Bridge
from telegram_bridge.config import ConfigError, load_config
from telegram_bridge.state import StateError, StateStore
from telegram_bridge.telegram import TelegramClient, TelegramError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify configuration, Telegram token, and local agent (read-only)")
    args = parser.parse_args()
    try:
        config = load_config()
        agent = AgentClient(config.executable, config.server_url, config.model, config.agent,
                            permission_mode=config.permission_mode, backend_mode=config.backend_mode,
                            server_auth=config.server_auth)
        telegram = TelegramClient(config.token)
        if args.check:
            info = agent.health()
            identity = telegram.get_me()
            webhook = telegram.webhook_info()
            if not isinstance(webhook, dict) or webhook.get("url"):
                raise ConfigError("Existing webhook found; it was not changed.")
            print(f"Telegram bot: @{identity.get('username', 'unknown')}\nBackend: {info['version']}\nOwner-only configuration: valid")
            return 0
        with StateStore(config.state_path) as state:
            bridge = Bridge(config, state, telegram, agent)
            signal.signal(signal.SIGTERM, lambda *_: bridge.stopping.set())
            bridge.run()
        return 0
    except (ConfigError, AgentError, StateError, TelegramError) as error:
        print(str(error))
        return 1
    except KeyboardInterrupt:
        print("\nBridge stopped. Completed work and operational state preserved.")
        return 0
    except Exception:
        # Tracebacks may include private payloads; don't emit them to shared logs.
        print("Bridge stopped after an internal error. No uncertain command was repeated.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
