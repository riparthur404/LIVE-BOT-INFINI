"""Gestion des cles de licence — generation, validation, admin."""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone

import discord

from config import ADMIN_IDS, VIP_KEY
from database import Database, UserLicense

db = Database()

DURATION_LABELS = {
    "lifetime": "Lifetime (n'expire jamais)",
    "1day": "1 jour",
    "1week": "1 semaine",
    "1month": "1 mois",
    "3months": "3 mois",
}

TYPE_LABELS = {
    "dualhook": "Dualhook (webhook actif)",
    "normal": "Sans dualhook (pas de webhook)",
}


def is_admin(interaction: discord.Interaction) -> bool:
    uid = str(interaction.user.id)
    if uid in ADMIN_IDS:
        return True
    if interaction.guild and interaction.user.guild_permissions.administrator:
        return True
    return False


def is_vip(key: str | None) -> bool:
    return (key or "").strip() == VIP_KEY


def generate_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    part = lambda: "".join(secrets.choice(alphabet) for _ in range(4))
    return f"FAZOX-{part()}-{part()}-{part()}"


def user_has_valid_license(discord_id: str) -> bool:
    lic = db.get_user_license(discord_id)
    return lic is not None and lic.valid


def user_dualhook_enabled(discord_id: str) -> bool:
    lic = db.get_user_license(discord_id)
    if not lic or not lic.valid:
        return False
    return lic.dualhook


def format_expiry(expires_at: str | None) -> str:
    if not expires_at:
        return "Lifetime"
    try:
        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp.strftime("%d/%m/%Y %H:%M UTC")
    except ValueError:
        return expires_at


def license_status_text(lic: UserLicense | None) -> str:
    if not lic:
        return "Aucune cle active. Utilise `/cle cle:FAZOX-...`"
    if not lic.valid:
        return f"Cle **{lic.license_key}** — {lic.reason}"
    kind = "dualhook" if lic.dualhook else "sans dualhook"
    exp = format_expiry(lic.expires_at)
    return f"Cle **{lic.license_key}** ({kind}) — expire: **{exp}**"