"""Discord bot notification helper."""

import logging
from datetime import datetime

import requests

from src.config import settings

logger = logging.getLogger(__name__)

_API_BASE = "https://discord.com/api/v10"


def send_discord(title: str, message: str, color: int = 0x760f10) -> None:
    """Send an embed to the configured Discord channel via bot token."""
    token = settings.discord_bot_token
    channel_id = settings.discord_channel_id
    if not token or not channel_id:
        return

    payload = {
        "embeds": [{
            "title": title,
            "description": message,
            "color": color,
            "footer": {"text": "CC Visual Trade"},
            "timestamp": datetime.utcnow().isoformat(),
        }]
    }
    try:
        requests.post(
            f"{_API_BASE}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}"},
            json=payload,
            timeout=10,
        ).raise_for_status()
    except Exception as e:
        logger.warning(f"Discord notification failed: {e}")
