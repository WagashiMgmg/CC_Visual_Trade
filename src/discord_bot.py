"""Discord bot: prefix commands for dashboard interaction."""

import asyncio
import io
import logging

import discord
from discord.ext import commands

from src.config import settings

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def _take_screenshot() -> bytes:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        await page.goto("http://localhost:8080", wait_until="networkidle", timeout=15000)
        data = await page.screenshot(full_page=True)
        await browser.close()
    return data


async def _notify_tunnel_url() -> None:
    """data/tunnel_url にURLが書かれていればDiscordに通知する。"""
    channel_id = settings.discord_channel_id
    if not channel_id:
        return
    try:
        url_file = "/app/data/tunnel_url"
        with open(url_file) as f:
            url = f.read().strip()
        if not url:
            return
        channel = bot.get_channel(int(channel_id))
        if channel:
            await channel.send(f"🚀 **Dashboard URL**: {url}")
            logger.info(f"Notified tunnel URL to Discord: {url}")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Tunnel URL notification failed: {e}")


@bot.event
async def on_ready():
    logger.info(f"Discord bot ready: {bot.user} (prefix=!)")
    await _notify_tunnel_url()


@bot.command(name="screenshot", aliases=["ss", "s"])
async def cmd_screenshot(ctx: commands.Context):
    """!screenshot — ダッシュボードのスクショを送信"""
    async with ctx.typing():
        try:
            data = await _take_screenshot()
            await ctx.send(file=discord.File(io.BytesIO(data), filename="dashboard.png"))
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            await ctx.send(f"❌ Screenshot failed: {e}")


async def start_bot() -> None:
    token = settings.discord_bot_token
    if not token:
        logger.info("DISCORD_BOT_TOKEN not set — Discord bot disabled")
        return
    try:
        await bot.start(token)
    except asyncio.CancelledError:
        await bot.close()
    except Exception as e:
        logger.error(f"Discord bot error: {e}")
