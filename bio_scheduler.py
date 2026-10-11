"""Rotation auto lien bio TikTok — 00h-12h lien perso, 12h-00h lien partenaire."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import PARTNER_BIO_LINK
from database import Database, StoredAccount
import tiktok_live

log = logging.getLogger(__name__)
db = Database()
TZ = ZoneInfo("Europe/Paris")
_last_slot: dict[str, str] = {}


def current_slot() -> str:
    """user = 00h00-11h59 | partner = 12h00-23h59 (heure Paris)."""
    hour = datetime.now(TZ).hour
    return "partner" if hour >= 12 else "user"


def bio_schedule_enabled(sess: dict) -> bool:
    return bool(sess.get("bio_schedule_enabled"))


def effective_bio_for_account(acc: StoredAccount, sess: dict) -> tuple[str, str]:
    """Retourne (bio_texte, lien) a appliquer sur TikTok maintenant."""
    original_link = (acc.original_bio_link or acc.bio_link or sess.get("bio_link") or "").strip()
    original_bio = (acc.bio or sess.get("bio") or "").strip()
    if current_slot() == "partner":
        return original_bio, PARTNER_BIO_LINK
    return original_bio, original_link


def _slot_key(discord_id: str, pseudo: str) -> str:
    return f"{discord_id}:{pseudo.lower().lstrip('@')}"


async def apply_scheduled_link_to_account(
    acc: StoredAccount,
    sess: dict,
    *,
    room_id: str | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """Applique le lien/bio planifie sur la room TikTok active."""
    if not acc.cookies_json:
        return False, "pas de cookie"
    pseudo = acc.account_pseudo.lower().lstrip("@")
    slot = current_slot()
    key = _slot_key(acc.discord_id, pseudo)
    if not force and _last_slot.get(key) == slot:
        return True, "deja a jour"

    bio_text, link = effective_bio_for_account(acc, sess)
    if not link:
        return False, "aucun lien configure"

    rid = room_id or acc.room_id
    if not rid:
        return False, "pas de room live"

    cookies = json.loads(acc.cookies_json)
    ok, err = await tiktok_live.update_room_bio_link(
        rid, cookies, link, bio=bio_text or None
    )
    if ok:
        _last_slot[key] = slot
        label = "partenaire" if slot == "partner" else "perso"
        return True, f"@{pseudo} → lien {label}"
    return False, err or "echec TikTok"


async def apply_scheduled_for_user(discord_id: str, *, force: bool = False) -> list[str]:
    """Applique la rotation sur tous les TikTok live du user."""
    from live_engine import _live_state, is_user_live

    did = str(discord_id)
    sess = db.get_session(did)
    if not bio_schedule_enabled(sess):
        return []
    if not is_user_live(did):
        return []

    st = _live_state(did)
    notes: list[str] = []
    for acc in st.accounts:
        if acc.platform != "tiktok" or not acc.cookies_json:
            continue
        db_acc = db.get_account("tiktok", acc.account_pseudo, did) or acc
        ok, msg = await apply_scheduled_link_to_account(
            db_acc, sess, room_id=acc.room_id or db_acc.room_id, force=force
        )
        notes.append(f"{'✅' if ok else '❌'} {msg}")
    return notes


async def enable_bio_schedule(discord_id: str, discord_name: str) -> tuple[bool, str]:
    """Active la rotation et sauvegarde les liens perso."""
    did = str(discord_id)
    accs = [a for a in db.list_accounts(did) if a.platform == "tiktok"]
    if not accs:
        return False, "Aucun compte TikTok. `/addtiktok` d'abord."

    saved = 0
    for acc in accs:
        personal = (acc.bio_link or "").strip()
        if personal and not (acc.original_bio_link or "").strip():
            db.set_original_bio_link(did, "tiktok", acc.account_pseudo, personal)
            saved += 1
        elif (acc.original_bio_link or "").strip():
            saved += 1

    db.save_session(did, discord_name, bio_schedule_enabled=True)
    slot = current_slot()
    window = "12h-00h partenaire" if slot == "partner" else "00h-12h perso"
    msg = (
        f"Rotation activee ({window}).\n"
        f"• **00h-12h** → lien + bio de la personne\n"
        f"• **12h-00h** → `{PARTNER_BIO_LINK}`\n"
        f"• Liens perso enregistres: **{saved}/{len(accs)}** compte(s)"
    )
    notes = await apply_scheduled_for_user(did, force=True)
    if notes:
        msg += "\n\n" + "\n".join(notes[:5])
    return True, msg


async def disable_bio_schedule(discord_id: str, discord_name: str) -> tuple[bool, str]:
    did = str(discord_id)
    db.save_session(did, discord_name, bio_schedule_enabled=False)
    _last_slot.clear()
    notes = []
    sess = db.get_session(did)
    from live_engine import _live_state, is_user_live

    if is_user_live(did):
        st = _live_state(did)
        for acc in st.accounts:
            if acc.platform != "tiktok" or not acc.cookies_json:
                continue
            db_acc = db.get_account("tiktok", acc.account_pseudo, did) or acc
            link = (db_acc.original_bio_link or db_acc.bio_link or "").strip()
            if not link:
                continue
            bio_text = (db_acc.bio or sess.get("bio") or "").strip()
            ok, err = await tiktok_live.update_room_bio_link(
                acc.room_id or db_acc.room_id,
                json.loads(acc.cookies_json),
                link,
                bio=bio_text or None,
            )
            notes.append(
                f"{'✅' if ok else '❌'} @{acc.account_pseudo} lien perso restaure"
                + (f" ({err})" if not ok and err else "")
            )
    msg = "Rotation desactivee."
    if notes:
        msg += "\n" + "\n".join(notes)
    return True, msg


def record_personal_bio_link(
    discord_id: str,
    pseudo: str,
    url: str,
    *,
    discord_name: str | None = None,
) -> None:
    """Enregistre le lien perso (restaure 00h-12h)."""
    pseudo = pseudo.lower().lstrip("@")
    db.set_original_bio_link(str(discord_id), "tiktok", pseudo, url)
    sess = db.get_session(discord_id)
    if not bio_schedule_enabled(sess) or current_slot() == "user":
        db.update_account_bio_link(
            pseudo, url, discord_id=str(discord_id), discord_name=discord_name
        )


async def scheduler_loop() -> None:
    """Verifie toutes les 60s les lives actifs avec rotation activee."""
    await asyncio.sleep(15)
    while True:
        try:
            from live_engine import _user_states, is_user_live

            for did in list(_user_states.keys()):
                if not is_user_live(did):
                    continue
                sess = db.get_session(did)
                if not bio_schedule_enabled(sess):
                    continue
                notes = await apply_scheduled_for_user(did)
                for line in notes:
                    if line.startswith("✅"):
                        log.info("Bio schedule %s: %s", did, line)
        except Exception:
            log.exception("bio_scheduler loop")
        await asyncio.sleep(60)


def start_bio_scheduler() -> asyncio.Task:
    return asyncio.create_task(scheduler_loop(), name="bio-scheduler")