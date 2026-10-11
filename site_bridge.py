"""Lovable site <-> Discord bot bridge (polling)."""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from discord import app_commands

log = logging.getLogger("site_bridge")

SITE_URL = os.getenv("SITE_URL", "https://gleam-grow-gems.lovable.app").rstrip("/")
BOT_API_TOKEN = os.getenv("BOT_API_TOKEN", "").strip()
POLL_INTERVAL = max(2, int(os.getenv("POLL_INTERVAL_SECONDS", "3")))
LIVE_VIDEO_DIR = Path(os.getenv("LIVE_VIDEO_DIR", "/home/container"))
ENABLE_LINK_COMMAND = os.getenv("ENABLE_LINK_COMMAND", "1").strip().lower() in {"1", "true", "yes", "on"}
ENABLED = bool(BOT_API_TOKEN)

_active: Dict[str, Dict[str, Any]] = {}
_heartbeat_task: Optional[asyncio.Task] = None
_poll_started = False


def _log(level: str, message: str, *args: Any) -> None:
    text = message % args if args else message
    print(f"[site_bridge] {level.upper()} {text}", flush=True)
    getattr(log, level, log.info)(message, *args)


def _headers() -> Dict[str, str]:
    return {"Content-Type": "application/json", "X-Bot-Token": BOT_API_TOKEN}


async def _request(method: str, path: str, payload: Optional[dict] = None) -> Optional[dict]:
    if not ENABLED:
        _log("warning", "disabled: BOT_API_TOKEN missing, cannot call %s", path)
        return None
    url = f"{SITE_URL}/api/public/bot{path}"
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; site-bridge/2.2)"},
        ) as s:
            async with s.request(method, url, json=payload, headers=_headers()) as r:
                text = await r.text()
                if r.status >= 400:
                    _log("warning", "bridge %s %s -> %s %s", method, path, r.status, text[:200])
                    return None
                try:
                    data = await r.json()
                    _log("info", "bridge %s %s -> %s", method, path, r.status)
                    return data
                except Exception:
                    _log("warning", "bridge %s %s returned non-json: %s", method, path, text[:200])
                    return {"raw": text}
    except Exception as e:
        _log("warning", "bridge %s %s failed: %s", method, path, e)
        return None


async def _post(path: str, payload: dict) -> Optional[dict]:
    return await _request("POST", path, payload)


async def _resolve_video_path(payload: dict) -> tuple[Optional[Path], Optional[str]]:
    """Local file, or download source_url from the Lovable dashboard."""
    video_name = (payload.get("video") or "").strip()
    source_url = (payload.get("source_url") or "").strip()

    if video_name:
        candidate = LIVE_VIDEO_DIR / video_name
        if candidate.is_file():
            return candidate, None

    if source_url.startswith(("http://", "https://")):
        LIVE_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        ext = ".mp4"
        for part in source_url.split("?")[0].split("/"):
            if "." in part:
                ext = "." + part.rsplit(".", 1)[-1].lower()[:8] or "mp4"
        if ext not in {".mp4", ".mov", ".mkv", ".webm"}:
            ext = ".mp4"
        dest = LIVE_VIDEO_DIR / f"site_{uuid.uuid4().hex[:12]}{ext}"
        try:
            timeout = aiohttp.ClientTimeout(total=300)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(source_url) as r:
                    if r.status >= 400:
                        return None, f"Download failed HTTP {r.status}"
                    with dest.open("wb") as fh:
                        async for chunk in r.content.iter_chunked(1024 * 256):
                            fh.write(chunk)
            if dest.stat().st_size < 1024:
                dest.unlink(missing_ok=True)
                return None, "Downloaded file too small"
            _log("info", "downloaded video -> %s (%s bytes)", dest.name, dest.stat().st_size)
            return dest, None
        except Exception as e:
            dest.unlink(missing_ok=True)
            return None, f"Download error: {e}"

    mp4s = sorted(LIVE_VIDEO_DIR.glob("*.mp4"))
    if mp4s:
        return mp4s[0], None

    return None, (
        f"Aucune vidéo dans {LIVE_VIDEO_DIR} "
        f"(source_url={'oui' if source_url else 'non'}, video='{video_name}')"
    )


async def notify_start(
    discord_id: str,
    accounts: Optional[List[Any]] = None,
    share_urls: Optional[List[str]] = None,
) -> None:
    if not ENABLED:
        return
    live_url = (share_urls or [None])[0]
    payload = {
        "discord_id": str(discord_id),
        "status": "live",
        "live_url": live_url,
        "viewer_url": live_url,
        "accounts": [getattr(a, "account_pseudo", str(a)) for a in (accounts or [])],
    }
    _active[str(discord_id)] = payload
    await _post("/session", payload)


async def notify_stop(discord_id: str) -> None:
    if not ENABLED:
        return
    _active.pop(str(discord_id), None)
    await _post("/session", {"discord_id": str(discord_id), "status": "stopped"})


async def _dispatch_command(bot: discord.Client, cmd: dict) -> None:
    cmd_id = cmd.get("id")
    kind = cmd.get("command")
    payload = cmd.get("payload") or {}
    discord_id = str(cmd.get("discord_id") or "").strip()
    user_id = str(cmd.get("user_id") or "").strip()
    actor_id = discord_id or user_id or "site"

    try:
        import live_engine
    except Exception as e:
        await _post("/commands/ack", {"id": cmd_id, "status": "error", "error": f"live_engine import: {e}"})
        return

    try:
        if kind == "start":
            _log("info", "start command %s (discord=%s)", cmd_id, discord_id or "none")
            title = str(payload.get("title") or "Live")
            platforms = list(payload.get("platforms") or ["tiktok"])
            admin_all = bool(payload.get("admin_all"))
            use_all_stock = bool(payload.get("use_all_stock"))

            video_path, video_err = await _resolve_video_path(payload)
            if video_path is None:
                await _post("/commands/ack", {
                    "id": cmd_id, "status": "error", "error": video_err or "no video",
                })
                await _post("/session", {
                    "user_id": user_id or None,
                    "discord_id": discord_id or None,
                    "status": "stopped",
                    "set_id": payload.get("set_id"),
                    "last_error": (video_err or "no video")[:500],
                })
                _log("warning", "command %s failed: %s", cmd_id, video_err)
                return

            ok, msg = await live_engine.start_live(
                discord_id=actor_id,
                video_path=video_path,
                platforms=platforms,
                title=title,
                admin_all=admin_all,
                use_all_stock=use_all_stock,
            )
            if ok:
                st = live_engine._live_state(actor_id)
                share_urls = list(st.share_urls.values()) if st.share_urls else []
                await _post("/session", {
                    "user_id": user_id or None,
                    "discord_id": discord_id or None,
                    "status": "live",
                    "set_id": payload.get("set_id"),
                    "live_url": share_urls[0] if share_urls else None,
                    "viewer_url": share_urls[0] if share_urls else None,
                })
            else:
                await _post("/session", {
                    "user_id": user_id or None,
                    "discord_id": discord_id or None,
                    "status": "stopped",
                    "set_id": payload.get("set_id"),
                    "last_error": msg[:500],
                })
            await _post("/commands/ack", {
                "id": cmd_id,
                "status": "done" if ok else "error",
                "result": {"message": msg, "video": str(video_path)},
                "error": None if ok else msg[:1000],
            })
            _log("info", "command %s finished ok=%s", cmd_id, ok)

        elif kind == "stop":
            _log("info", "stop command %s (discord=%s)", cmd_id, discord_id or "none")
            await live_engine.stop_live(discord_id=actor_id)
            await _post("/session", {
                "user_id": user_id or None,
                "discord_id": discord_id or None,
                "status": "stopped",
                "set_id": payload.get("set_id"),
            })
            await _post("/commands/ack", {"id": cmd_id, "status": "done", "result": {"message": "stopped"}})

        else:
            await _post("/commands/ack", {"id": cmd_id, "status": "error", "error": f"unknown command: {kind}"})
    except Exception as e:
        tb = traceback.format_exc()
        _log("error", "dispatch error for %s: %s\n%s", cmd_id, e, tb)
        await _post("/commands/ack", {
            "id": cmd_id,
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "result": {"traceback": tb[-1500:]},
        })


async def _poll_loop(bot: discord.Client) -> None:
    _log("info", "poll loop started (SITE_URL=%s, interval=%ss)", SITE_URL, POLL_INTERVAL)
    while True:
        try:
            data = await _post("/commands/next", {"max": 10})
            commands = (data or {}).get("commands") or []
            if commands:
                _log("info", "poll: %s command(s)", len(commands))
            for cmd in commands:
                asyncio.create_task(_dispatch_command(bot, cmd))
        except Exception as e:
            _log("warning", "poll error: %s", e)
        await asyncio.sleep(POLL_INTERVAL)


async def _heartbeat_loop() -> None:
    while True:
        try:
            for did, info in list(_active.items()):
                await _post("/heartbeat", {"discord_id": did, "live_url": info.get("live_url")})
        except Exception as e:
            _log("warning", "heartbeat error: %s", e)
        await asyncio.sleep(30)


def register(bot: discord.Client) -> None:
    global _heartbeat_task, _poll_started
    if not ENABLED:
        _log("warning", "disabled: set BOT_API_TOKEN in .env")
        return

    if ENABLE_LINK_COMMAND:

        @bot.tree.command(name="link", description="Lie ton Discord à ton compte Fazox sur le site")
        @app_commands.describe(code="Code à 6 caractères depuis /discord sur le site")
        async def link_cmd(interaction: discord.Interaction, code: str):
            await interaction.response.defer(ephemeral=True)
            data = await _post("/link", {
                "code": code.strip().upper(),
                "discord_id": str(interaction.user.id),
                "discord_username": str(interaction.user),
            })
            if data and data.get("ok"):
                await interaction.followup.send(
                    f"✅ Lié ! Lance un live depuis le dashboard : {SITE_URL}/dashboard",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"❌ Code invalide ou expiré. Génère-en un sur {SITE_URL}/discord",
                    ephemeral=True,
                )

    loop = bot.loop
    if _heartbeat_task is None:
        _heartbeat_task = loop.create_task(_heartbeat_loop())
    if not _poll_started:
        _poll_started = True
        loop.create_task(_poll_loop(bot))
    _log("info", "registered (SITE_URL=%s, poll=%ss, /link=%s)", SITE_URL, POLL_INTERVAL, ENABLE_LINK_COMMAND)