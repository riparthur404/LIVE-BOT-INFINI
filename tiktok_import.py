"""Import batch de comptes TikTok (plusieurs .txt ou .zip)."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import discord

from cookie_parser import parse_cookies
from live_engine import register_tiktok_from_cookies, resolve_tiktok_pseudo

GENERIC_STEMS = frozenset({"cookies", "cookie", "tiktok", "export", "account", "compte"})


def pseudo_from_filename(filename: str) -> str:
    stem = Path(filename).stem.strip().lstrip("@")
    if not stem or stem.lower() in GENERIC_STEMS:
        return ""
    return stem


def _read_txt_bytes(data: bytes, source_name: str) -> tuple[dict[str, str] | None, str | None]:
    raw = data.decode("utf-8", errors="ignore")
    parsed = parse_cookies(raw)
    if not parsed.success or not parsed.cookies:
        return None, parsed.error or f"Cookies invalides ({source_name})"
    return parsed.cookies, None


async def _import_one(
    discord_id: str,
    discord_name: str,
    *,
    cookies: dict[str, str],
    pseudo: str,
    source: str,
) -> tuple[str | None, str | None]:
    """Retourne (pseudo_ok, erreur)."""
    pseudo_clean = (pseudo or "").strip().lstrip("@")
    if not pseudo_clean:
        pseudo_clean = await resolve_tiktok_pseudo(cookies)
    if not pseudo_clean:
        return None, f"{source}: pseudo manquant (nomme le fichier @pseudo.txt ou ajoute sessionid)"
    try:
        acc, err, _rtmp, used, _created = await register_tiktok_from_cookies(
            discord_id,
            discord_name,
            cookies,
            pseudo_clean,
        )
    except Exception as exc:
        return None, f"@{pseudo_clean}: {exc}"
    if err:
        if err == "NEED_PSEUDO":
            return None, f"{source}: pseudo introuvable — nomme le fichier @pseudo.txt"
        return None, f"@{pseudo_clean}: {err}"
    return used or acc.account_pseudo, None


async def import_from_bytes(
    discord_id: str,
    discord_name: str,
    data: bytes,
    filename: str,
    pseudo_override: str = "",
) -> tuple[list[str], list[str]]:
    """Importe un .txt ou .zip. Retourne (pseudos_ok, erreurs)."""
    ok: list[str] = []
    errors: list[str] = []
    lower = (filename or "").lower()

    if lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [
                    n
                    for n in zf.namelist()
                    if not n.endswith("/")
                    and n.lower().endswith((".txt", ".json", ".cookies"))
                ]
                if not names:
                    return [], ["ZIP vide — mets des .txt/.json (un par compte)."]
                for name in names:
                    try:
                        blob = zf.read(name)
                    except Exception as exc:
                        errors.append(f"{name}: {exc}")
                        continue
                    cookies, err = _read_txt_bytes(blob, name)
                    if err or not cookies:
                        errors.append(err or name)
                        continue
                    pseudo = pseudo_override or pseudo_from_filename(Path(name).name)
                    saved, imp_err = await _import_one(
                        discord_id,
                        discord_name,
                        cookies=cookies,
                        pseudo=pseudo,
                        source=name,
                    )
                    if saved:
                        ok.append(saved)
                    elif imp_err:
                        errors.append(imp_err)
        except zipfile.BadZipFile:
            return [], ["Fichier ZIP invalide."]
        return ok, errors

    if lower.endswith((".json", ".cookies")):
        cookies, err = _read_txt_bytes(data, filename)
        if err or not cookies:
            return [], [err or "JSON cookies invalide"]
        pseudo = pseudo_override or pseudo_from_filename(filename)
        saved, imp_err = await _import_one(
            discord_id,
            discord_name,
            cookies=cookies,
            pseudo=pseudo,
            source=filename,
        )
        if saved:
            return [saved], []
        return [], [imp_err or "Import echoue"]

    if not lower.endswith(".txt"):
        return [], ["Format non supporte — envoie .txt, .json ou .zip"]

    cookies, err = _read_txt_bytes(data, filename)
    if err or not cookies:
        return [], [err or "Cookies invalides"]
    pseudo = pseudo_override or pseudo_from_filename(filename)
    saved, imp_err = await _import_one(
        discord_id,
        discord_name,
        cookies=cookies,
        pseudo=pseudo,
        source=filename,
    )
    if saved:
        ok.append(saved)
    elif imp_err:
        errors.append(imp_err)
    return ok, errors


@dataclass
class PendingFile:
    filename: str
    data: bytes


@dataclass
class MultiImportSession:
    discord_id: str
    discord_name: str
    channel_id: int
    files: list[PendingFile] = field(default_factory=list)
    active: bool = True


_active_sessions: dict[str, MultiImportSession] = {}


def get_import_session(discord_id: str) -> MultiImportSession | None:
    return _active_sessions.get(discord_id)


def start_import_session(discord_id: str, discord_name: str, channel_id: int) -> MultiImportSession:
    session = MultiImportSession(discord_id, discord_name, channel_id)
    _active_sessions[discord_id] = session
    return session


def end_import_session(discord_id: str) -> None:
    _active_sessions.pop(discord_id, None)


def _channel_accepts_drop(session: MultiImportSession, message: discord.Message) -> bool:
    if message.channel.id == session.channel_id:
        return True
    return isinstance(message.channel, discord.DMChannel)


async def collect_message_attachments(message: discord.Message) -> int:
    """Ajoute les .txt/.zip d'un message a la session active. Retourne le nombre ajoute."""
    if message.author.bot or not message.attachments:
        return 0
    session = _active_sessions.get(str(message.author.id))
    if not session or not session.active:
        return 0
    if not _channel_accepts_drop(session, message):
        return 0

    added = 0
    for att in message.attachments:
        name = att.filename or ""
        lower = name.lower()
        if not (
            lower.endswith(".txt")
            or lower.endswith(".zip")
            or lower.endswith(".json")
            or lower.endswith(".cookies")
        ):
            continue
        data = await att.read()
        session.files.append(PendingFile(filename=name, data=data))
        added += 1
    return added


async def import_file_list(
    discord_id: str,
    discord_name: str,
    files: list[tuple[str, bytes]],
) -> tuple[list[str], list[str]]:
    all_ok: list[str] = []
    all_errors: list[str] = []
    for filename, data in files:
        ok, errors = await import_from_bytes(discord_id, discord_name, data, filename)
        all_ok.extend(ok)
        all_errors.extend(errors)
    return all_ok, all_errors


async def import_attachments(
    discord_id: str,
    discord_name: str,
    attachments: list[discord.Attachment],
    pseudo_override: str = "",
) -> tuple[list[str], list[str]]:
    all_ok: list[str] = []
    all_errors: list[str] = []
    for att in attachments:
        if not att.filename:
            all_errors.append("Fichier sans nom ignore")
            continue
        data = await att.read()
        ok, errors = await import_from_bytes(
            discord_id,
            discord_name,
            data,
            att.filename,
            pseudo_override=pseudo_override if len(attachments) == 1 else "",
        )
        all_ok.extend(ok)
        all_errors.extend(errors)
    return all_ok, all_errors