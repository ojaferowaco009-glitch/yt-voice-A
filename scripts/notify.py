"""Minimal Telegram notifier, mirroring yt-core's scripts/lib/notify.py so
messages look/behave the same across both repos. Kept as a tiny standalone
copy rather than a shared package -- these two repos are deliberately
independent (separate GitHub accounts), so no shared dependency between
them.
"""
from __future__ import annotations
import os
import sys
import requests

_TELEGRAM_MAX_CHARS = 4096


def send(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"(Telegram not configured) {message}")
        return
    if len(message) > _TELEGRAM_MAX_CHARS:
        message = message[: _TELEGRAM_MAX_CHARS - 20] + "\n...[truncated]"
    try:
        res = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": chat_id, "text": message}, timeout=20)
        res.raise_for_status()
    except requests.RequestException as exc:
        print(f"Telegram notification failed: {exc}")


if __name__ == "__main__":
    send(sys.argv[1] if len(sys.argv) > 1 else "(no message)")
