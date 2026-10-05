"""Small terminal UI; no dependencies, secret echo, or immortal spinner threads."""
from __future__ import annotations

from contextlib import contextmanager
import getpass
import os
import re
import sys
import threading


class TerminalError(RuntimeError):
    pass


def safe_text(value) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(value))[:1000]


class UI:
    def __init__(self, plain=False, stream=None):
        self.stream = stream or sys.stdout
        self.animated = not plain and self.stream.isatty() and "NO_COLOR" not in os.environ

    def say(self, message, style=""):
        text = safe_text(message)
        color = {"title": "36;1", "ok": "32", "warn": "33", "error": "31"}.get(style)
        if self.animated and color:
            text = f"\033[{color}m{text}\033[0m"
        print(text, file=self.stream, flush=True)

    def title(self, message):
        self.say(message, "title")

    def prompt(self, message, default=None):
        suffix = f" [{safe_text(default)}]" if default is not None else ""
        value = input(safe_text(message) + suffix + ": ").strip()
        return value if value else (str(default) if default is not None else "")

    def confirm(self, message, default=False):
        while True:
            answer = self.prompt(message + (" [Y/n]" if default else " [y/N]"))
            if not answer:
                return default
            if answer.lower() in ("yes", "y"):
                return True
            if answer.lower() in ("no", "n"):
                return False
            self.say("Please answer yes or no.")

    def choose(self, message, choices, default=0):
        self.title(message)
        for i, label in enumerate(choices, 1):
            self.say(f"  {i}. {label}")
        while True:
            value = self.prompt("Selection", str(default + 1))
            if value.isascii() and value.isdigit() and 1 <= int(value) <= len(choices):
                return int(value) - 1
            self.say("Choose a listed number.")

    def secret(self, message):
        require_terminal()
        return getpass.getpass(safe_text(message) + ": ")

    @contextmanager
    def busy(self, message):
        self.say(message)
        stop = threading.Event()
        thread = None
        if self.animated:
            def animate():
                frames = "|/-\\"
                index = 0
                while not stop.wait(0.12):
                    print("\r" + frames[index % 4] + " " + safe_text(message),
                          end="", file=self.stream, flush=True)
                    index += 1
            thread = threading.Thread(target=animate, name="omnirush-progress", daemon=True)
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=1)
                print("\r\033[2K", end="", file=self.stream, flush=True)


def require_terminal():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise TerminalError("Setup/login requires an interactive terminal; credentials are never accepted in arguments or pipes.")
