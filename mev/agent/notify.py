"""Alerts: macOS notification and an optional webhook (Slack, Discord or Telegram, detected from the URL)."""
import logging
import subprocess
import sys

import aiohttp

log = logging.getLogger("mev.agent.notify")


def macos_notify(title: str, text: str) -> bool:
    if sys.platform != "darwin":
        return False
    safe = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(["osascript", "-e", f'display notification "{safe(text)}" with title "{safe(title)}"'],
                       capture_output=True, timeout=10)
    return r.returncode == 0


def webhook_payload(url: str, text: str) -> dict:
    if "discord.com/api/webhooks" in url:
        return {"content": text}
    if "api.telegram.org" in url:
        return {"text": text}          # chat_id goes in the URL query string
    return {"text": text}              # Slack incoming webhook and anything Slack-shaped


async def webhook(url: str, text: str) -> tuple[bool, str]:
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=webhook_payload(url, text), timeout=aiohttp.ClientTimeout(total=15)) as r:
            body = (await r.text())[:200]
            return r.status < 300, f"{r.status} {body}"
