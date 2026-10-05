# Security policy

## Report privately

Do not open a public issue containing a Telegram token, provider credential, private project file, native auth data, or raw logs. Revoke leaked Telegram tokens with BotFather immediately, then report the vulnerability privately to the repository owner.

## Operational rules

- Run as an unprivileged dedicated Linux user; never use `sudo ./setup.sh`.
- Keep `~/.config/omnirush-telegram-portable`, `~/.local/state/omnirush-telegram-portable`, and `~/.local/share/omnirush-telegram-portable` private.
- Use a narrow project root and review `ASK` permission prompts.
- Treat `FULL` as unrestricted tool access within the Linux user’s existing privileges.
- Do not expose the native service beyond loopback or place tokens in shell history.
- Telegram is not end-to-end encrypted; do not send secrets through the bot.
