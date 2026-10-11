"""Orchestration multi-plateforme."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from config import (
    DEFAULT_RTMP,
    DIAMOND_TO_EUR,
    MAX_LIVE_ACCOUNTS,
    PLATFORM_LABELS,
    PREP_CONCURRENCY,
    RTMP_CHUNK_SIZE,
)
from database import Database, StoredAccount
import tiktok_live
from overlay_config import draft_from_session, load_config
from overlay_render import OverlayPlacement, overlay_png_path, render_overlay_png
from streamer import MultiFfmpegPool
from license import user_dualhook_enabled
from webhook_notify import notify_account_saved

log = logging.getLogger(__name__)
db = Database()
_user_states: dict[str, LiveState] = {}
_user_streamers: dict[str, MultiFfmpegPool] = {}
_viewer_cache: dict[str, int] = {}
_viewer_task: asyncio.Task | None = None
ALL_STREAM_PLATFORMS = ("tiktok", "twitch", "youtube", "kick")


@dataclass
class LiveState:
    active: bool = False
    platforms: list[str] = field(default_factory=list)
    accounts: list[StoredAccount] = field(default_factory=list)
    share_urls: dict[str, str] = field(default_factory=dict)


def _live_state(discord_id: str) -> LiveState:
    did = str(discord_id)
    if did not in _user_states:
        _user_states[did] = LiveState()
    return _user_states[did]


def _streamer(discord_id: str) -> MultiFfmpegPool:
    did = str(discord_id)
    if did not in _user_streamers:
        _user_streamers[did] = MultiFfmpegPool()
    return _user_streamers[did]


def is_user_live(discord_id: str) -> bool:
    return _live_state(discord_id).active


# Compat ancien code
state = _live_state("__global__")


def _any_live_active() -> bool:
    return any(s.active for s in _user_states.values())


async def _poll_viewers_loop() -> None:
    while _any_live_active():
        for did, st in list(_user_states.items()):
            if not st.active:
                continue
            for acc in st.accounts:
                if acc.platform != "tiktok" or not acc.cookies_json or not acc.room_id:
                    continue
                cookies = json.loads(acc.cookies_json)
                count = await tiktok_live.fetch_room_viewers(acc.room_id, cookies)
                if count is not None:
                    _viewer_cache[f"{did}:{acc.platform}:{acc.account_pseudo}"] = count
        await asyncio.sleep(12)


def _start_viewer_poll() -> None:
    global _viewer_task
    if _viewer_task and not _viewer_task.done():
        return
    _viewer_task = asyncio.create_task(_poll_viewers_loop())


def _stop_viewer_poll_if_idle() -> None:
    global _viewer_task
    if _any_live_active():
        return
    if _viewer_task and not _viewer_task.done():
        _viewer_task.cancel()
    _viewer_task = None
    _viewer_cache.clear()


async def apply_profile_live(
    discord_id: str,
    *,
    title: str | None = None,
    bio: str | None = None,
    bio_link: str | None = None,
) -> str | None:
    """Applique titre/bio/lien sur toutes les rooms TikTok actives via leurs cookies."""
    st = _live_state(discord_id)
    if not st.active:
        return None
    sess = db.get_session(discord_id)
    title = title if title is not None else sess.get("live_title")
    bio = bio if bio is not None else sess.get("bio")
    bio_link = bio_link if bio_link is not None else sess.get("bio_link")
    notes: list[str] = []
    for acc in st.accounts:
        if acc.platform != "tiktok":
            continue
        if bio_link:
            notes.append(
                f"⏳ @{acc.account_pseudo} — lien enregistre, actif au prochain /startall"
            )
        elif title:
            notes.append(
                f"⏳ @{acc.account_pseudo} — titre enregistre, actif au prochain /startall"
            )
    return "\n".join(notes) if notes else None


async def _prepare_tiktok_account(
    acc: StoredAccount,
    title: str,
    bio: str = "",
    bio_link: str = "",
    *,
    no_skip: bool = True,
) -> tuple[StoredAccount | None, str | None, str | None]:
    """Cree/rafraichit une room TikTok. Si no_skip: fallback cle RTMP deja enregistree."""
    if not acc.cookies_json:
        # Fallback: cle manuelle deja en DB
        if no_skip and rtmp_target(acc):
            return acc, None, None
        return None, "Pas de cookies", None
    cookies = json.loads(acc.cookies_json)
    domain = await tiktok_live.resolve_webcast_domain(cookies)
    room = None
    last_err: str | None = None
    max_attempts = 5
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(2 + attempt * 2)
        room = await tiktok_live.create_live_room(
            title,
            cookies,
            domain=domain,
            bio=bio or None,
            bio_link=bio_link or None,
        )
        if room.success and room.full_url:
            break
        last_err = room.error or "Room TikTok echouee"
        log.warning(
            "TikTok room @%s tentative %s/%s: %s",
            acc.account_pseudo,
            attempt + 1,
            max_attempts,
            last_err,
        )

    if not room or not room.success or not room.full_url:
        # Ne pas skip: reutilise la derniere cle RTMP connue du compte
        if no_skip:
            fallback = rtmp_target(acc)
            if fallback:
                log.warning(
                    "TikTok @%s: room fail (%s) — reuse RTMP sauvegardee",
                    acc.account_pseudo,
                    last_err,
                )
                return acc, None, None
        return None, last_err or "Room TikTok echouee", None

    resolved_domain = room.domain or domain
    updated, _ = db.upsert_account(
        discord_id=acc.discord_id,
        discord_name=acc.discord_name,
        platform="tiktok",
        account_pseudo=acc.account_pseudo,
        cookies=cookies,
        stream_key=room.stream_key or "",
        rtmp_url=room.rtmp_url or "",
        full_push_url=room.full_url or "",
        room_id=room.room_id or "",
    )
    if room.room_id:
        tiktok_live.start_anchor_ping(
            room.room_id, cookies, resolved_domain, acc.account_pseudo
        )
        tiktok_live.start_visibility_boost(
            room.room_id, cookies, resolved_domain, acc.account_pseudo
        )
        await tiktok_live.bypass_visibility_algorithm(
            cookies, room_id=room.room_id, domain=resolved_domain
        )
    return updated, None, room.share_url


def resolve_overlay_for_live(discord_id: str, sess: dict) -> OverlayPlacement | None:
    """Retourne le PNG overlay compact + position (config sauvegardee ou brouillon)."""
    cfg = None
    active = (sess.get("overlay_config_name") or "").strip()
    if active:
        cfg = load_config(discord_id, active)
    if not cfg:
        draft = draft_from_session(sess)
        if draft.texts or draft.images:
            cfg = draft
    if not cfg or (not cfg.texts and not cfg.images):
        return None
    name = cfg.name or active or "live"
    out = overlay_png_path(discord_id, name)
    try:
        placement = render_overlay_png(cfg, discord_id, out)
    except (ValueError, OSError) as exc:
        log.warning("Overlay live ignore: %s", exc)
        return None
    return placement if placement.path.is_file() else None


def rtmp_target(acc: StoredAccount) -> str | None:
    if acc.full_push_url:
        return acc.full_push_url
    if acc.stream_key.startswith(("rtmp://", "rtmps://")):
        return acc.stream_key
    base = acc.rtmp_url or DEFAULT_RTMP.get(acc.platform, "")
    if not base or not acc.stream_key:
        return None
    return base.rstrip("/") + "/" + acc.stream_key.lstrip("/")


def _find_live_tiktok_account(discord_id: str, pseudo: str | None = None) -> StoredAccount | None:
    st = _live_state(discord_id)
    tiktok_live_accs = [a for a in st.accounts if a.platform == "tiktok" and a.cookies_json]
    if pseudo:
        key = pseudo.strip().lstrip("@").lower()
        for acc in tiktok_live_accs:
            if acc.account_pseudo.lower() == key:
                return acc
        acc = db.get_account("tiktok", key, discord_id)
        return acc if acc and acc.cookies_json else None
    return tiktok_live_accs[0] if tiktok_live_accs else None


async def fetch_tiktok_gift_report(
    discord_id: str,
    pseudo: str | None = None,
) -> tuple[bool, str]:
    """Stats cadeaux / euros pour un compte TikTok (live en cours ou room enregistree)."""
    did = str(discord_id)
    acc = _find_live_tiktok_account(did, pseudo)
    if not acc:
        accs = [a for a in db.list_accounts(did) if a.platform == "tiktok" and a.cookies_json]
        if pseudo:
            key = pseudo.strip().lstrip("@").lower()
            acc = next((a for a in accs if a.account_pseudo.lower() == key), None)
        elif accs:
            acc = accs[0]
    if not acc or not acc.cookies_json:
        return False, "Aucun compte TikTok. `/addtiktok` d'abord."

    cookies = json.loads(acc.cookies_json)
    room_id = acc.room_id or ""
    if not room_id and is_user_live(did):
        st = _live_state(did)
        live_acc = next(
            (a for a in st.accounts if a.platform == "tiktok" and a.account_pseudo == acc.account_pseudo),
            None,
        )
        if live_acc and live_acc.room_id:
            room_id = live_acc.room_id

    stats = await tiktok_live.fetch_gift_stats(acc.account_pseudo, cookies, room_id or None)
    if not stats:
        return False, f"Impossible de lire les stats pour @{acc.account_pseudo}."

    live_diamonds = stats.diamonds or 0
    live_eur = round(live_diamonds * DIAMOND_TO_EUR, 2)
    wallet_diamonds = stats.wallet_diamonds
    wallet_eur = round(wallet_diamonds * DIAMOND_TO_EUR, 2) if wallet_diamonds is not None else None

    lines = [
        f"**TikTok @{acc.account_pseudo}**",
        f"• Live: {'🔴 EN COURS' if stats.is_live or is_user_live(did) else '⚪ hors live'}",
    ]
    if room_id or stats.room_id:
        lines.append(f"• Room: `{room_id or stats.room_id}`")
    if stats.viewers is not None:
        lines.append(f"• Viewers: **{stats.viewers}**")
    lines.append(f"• Diamants (ce live): **{live_diamonds:,}**".replace(",", " "))
    lines.append(f"• Estimation live: **~{live_eur:.2f} €**")
    if stats.gifts_count is not None:
        lines.append(f"• Cadeaux envoyes: **{stats.gifts_count}**")
    if wallet_diamonds is not None:
        lines.append(f"• Solde compte: **{wallet_diamonds:,}** diamants".replace(",", " "))
        if wallet_eur is not None:
            lines.append(f"• Solde estime: **~{wallet_eur:.2f} €**")
    else:
        lines.append("• Solde compte: non disponible via API")
    lines.append(f"\n_(1 diamant ≈ {DIAMOND_TO_EUR:.4f} € createur)_")
    return True, "\n".join(lines)


async def update_link_live_now(
    discord_id: str,
    discord_name: str,
    url: str,
    pseudo: str,
) -> tuple[bool, str]:
    """Change le lien bio TikTok en direct pendant le live."""
    norm_url, url_err = tiktok_live.normalize_bio_url(url)
    if url_err or not norm_url:
        return False, url_err or "URL invalide."

    pseudo = pseudo.strip().lstrip("@").lower()
    acc = db.get_account("tiktok", pseudo, discord_id)
    if not acc or not acc.cookies_json:
        return False, f"Compte @{pseudo} introuvable. `/addtiktok` d'abord."

    cookies = json.loads(acc.cookies_json)
    room_id = acc.room_id or ""
    if is_user_live(discord_id):
        st = _live_state(discord_id)
        live_acc = next(
            (a for a in st.accounts if a.platform == "tiktok" and a.account_pseudo.lower() == pseudo),
            None,
        )
        if live_acc and live_acc.room_id:
            room_id = live_acc.room_id

    if not room_id:
        return False, "Pas de live actif sur ce compte. Lance `/livetiktok` ou `/startall`."

    sess = db.get_session(discord_id)
    bio = sess.get("bio") or acc.bio or ""
    ok, err = await tiktok_live.update_room_bio_link(
        room_id, cookies, norm_url, bio=bio or None
    )
    if not ok:
        if err and "url does not match" in str(err).lower():
            return False, (
                f"{err}\n\nFormat: `https://discord.gg/tonserveur`\n"
                "Ajoute aussi le lien dans ton profil TikTok."
            )
        return False, err or "TikTok a refuse le changement en direct."

    from bio_scheduler import bio_schedule_enabled, current_slot, record_personal_bio_link

    record_personal_bio_link(discord_id, pseudo, norm_url, discord_name=discord_name)
    sess_now = db.get_session(discord_id)
    if not bio_schedule_enabled(sess_now) or current_slot() == "user":
        db.save_session(discord_id, discord_name, bio_link=norm_url)
    db.log_cookie_action(
        discord_id=discord_id,
        discord_name=discord_name,
        account_pseudo=pseudo,
        action="lien_live",
        bio_link=norm_url,
        cookies=cookies,
    )
    return True, f"Lien bio @{pseudo} change **en direct**.\nURL: {norm_url}"


async def update_link_via_cookie(
    discord_id: str,
    discord_name: str,
    url: str,
    pseudo: str,
    cookies: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Change le lien bio TikTok via le cookie enregistre ou fourni."""
    from tiktok_live import normalize_bio_url

    norm_url, url_err = normalize_bio_url(url)
    if url_err or not norm_url:
        return False, url_err or "URL invalide."

    url = norm_url
    pseudo = pseudo.strip().lstrip("@").lower()
    acc = db.get_account("tiktok", pseudo, discord_id)

    if cookies:
        pass
    elif acc and acc.cookies_json:
        cookies = json.loads(acc.cookies_json)
    else:
        return False, f"Aucun cookie pour @{pseudo}. `/live cookies` ou `/live lien_cookie_form`."

    room_id = acc.room_id if acc else ""
    stream_key = acc.stream_key if acc else ""
    rtmp_url = acc.rtmp_url if acc else ""
    full_push = acc.full_push_url if acc else ""
    deferred_live = False
    tiktok_applied = False

    # TikTok refuse webcast/room/update/ pour les liens (erreur "Url does not match").
    # Hors live : on applique via create_live_room puis on ferme la room test.
    if not is_user_live(discord_id):
        domain = await tiktok_live.resolve_webcast_domain(cookies)
        room = await tiktok_live.create_live_room(
            acc.live_title if acc else "Live",
            cookies,
            domain=domain,
            bio_link=url,
        )
        if not room.success:
            err = room.error or "Impossible d'appliquer le lien via cookie"
            if "url does not match" in str(err).lower():
                return False, (
                    f"{err}\n\n"
                    "Format attendu: `https://discord.gg/tonserveur`\n"
                    "Ajoute aussi ce lien dans ton profil TikTok (Modifier le profil > Lien)."
                )
            return False, err
        tiktok_applied = True
        room_id = room.room_id or room_id
        stream_key = room.stream_key or stream_key
        rtmp_url = room.rtmp_url or rtmp_url
        full_push = room.full_url or full_push
        if room.room_id:
            await tiktok_live.end_live_room(cookies, domain)
    else:
        domain = await tiktok_live.resolve_webcast_domain(cookies)
        live_room_id = room_id
        if is_user_live(discord_id):
            st = _live_state(discord_id)
            live_acc = next(
                (a for a in st.accounts if a.platform == "tiktok" and a.account_pseudo.lower() == pseudo),
                None,
            )
            if live_acc and live_acc.room_id:
                live_room_id = live_acc.room_id
        if live_room_id:
            ok_live, err_live = await tiktok_live.update_room_bio_link(
                live_room_id,
                cookies,
                url,
                bio=acc.bio if acc else None,
                domain=domain,
            )
            if ok_live:
                tiktok_applied = True
                deferred_live = False
            else:
                deferred_live = True
                log.warning("Lien live @%s: %s", pseudo, err_live)
        else:
            deferred_live = True

    from bio_scheduler import record_personal_bio_link

    record_personal_bio_link(discord_id, pseudo, url, discord_name=discord_name)
    db.update_profile(discord_id, bio_link=url)
    sess_now = db.get_session(discord_id)
    from bio_scheduler import bio_schedule_enabled, current_slot

    if not bio_schedule_enabled(sess_now) or current_slot() == "user":
        db.save_session(discord_id, discord_name, bio_link=url)
    db.log_cookie_action(
        discord_id=discord_id,
        discord_name=discord_name,
        account_pseudo=pseudo,
        action="lien_update",
        bio_link=url,
        cookies=cookies,
    )

    db.upsert_account(
        discord_id=discord_id,
        discord_name=discord_name,
        platform="tiktok",
        account_pseudo=pseudo,
        cookies=cookies,
        stream_key=stream_key,
        rtmp_url=rtmp_url,
        full_push_url=full_push,
        room_id=room_id,
    )
    msg = f"Lien bio @{pseudo} enregistre.\nURL: {url}"
    if tiktok_applied:
        msg += "\n✅ Applique sur TikTok en direct." if is_user_live(discord_id) else "\n✅ Applique sur TikTok (hors live)."
    elif deferred_live:
        msg += (
            "\n⏳ Live en cours — utilise `/liendirect code:fazox1234` pour forcer en direct, "
            "ou le lien sera applique au prochain `/startall`."
        )
    return True, msg


async def update_all_tiktok_bio_links(
    discord_id: str,
    discord_name: str,
    url: str,
) -> tuple[bool, str]:
    """Applique le meme lien bio sur tous les comptes TikTok de l'utilisateur."""
    accs = [
        a
        for a in db.list_accounts(discord_id)
        if a.platform == "tiktok" and a.cookies_json
    ]
    if not accs:
        return False, "Aucun compte TikTok enregistre. `/live cookies` d'abord."

    ok_count = 0
    errors: list[str] = []
    for acc in accs:
        ok, msg = await update_link_via_cookie(
            discord_id, discord_name, url, acc.account_pseudo
        )
        if ok:
            ok_count += 1
        else:
            errors.append(f"@{acc.account_pseudo}: {msg}")

    if ok_count == 0:
        return False, "\n".join(errors[:5]) if errors else "Aucun compte mis a jour."

    out = f"Lien mis a jour sur **{ok_count}/{len(accs)}** compte(s) TikTok.\nURL: {url}"
    if errors:
        out += f"\n\nEchecs ({len(errors)}):\n" + "\n".join(errors[:5])
    return True, out


async def update_platform_bio_link(
    discord_id: str,
    discord_name: str,
    platform: str,
    pseudo: str,
    url: str,
) -> tuple[bool, str]:
    """Lien bio — TikTok via API cookie, autres plateformes en DB."""
    platform = platform.lower()
    if platform == "tiktok":
        return await update_link_via_cookie(discord_id, discord_name, url, pseudo)
    from tiktok_live import normalize_bio_url

    norm_url, url_err = normalize_bio_url(url)
    if url_err or not norm_url:
        return False, url_err or "URL invalide."
    acc = db.get_account(platform, pseudo, discord_id)
    if not acc:
        return False, f"Compte **{platform}** @{pseudo} introuvable dans ton panel."
    db.update_account_fields(
        discord_id, platform, pseudo, bio_link=norm_url, discord_name=discord_name
    )
    db.save_session(discord_id, discord_name, bio_link=norm_url)
    label = PLATFORM_LABELS.get(platform, platform)
    return True, (
        f"Lien **{label}** @{pseudo} enregistre.\nURL: {norm_url}\n"
        f"Applique-le aussi sur ton profil {label} (Dashboard > Profil)."
    )


async def update_platform_bio(
    discord_id: str,
    discord_name: str,
    platform: str,
    pseudo: str,
    bio_text: str,
) -> tuple[bool, str]:
    """Texte bio — session + compte + push TikTok si cookies."""
    platform = platform.lower()
    text = (bio_text or "").strip()
    if not text:
        return False, "Bio vide."
    acc = db.get_account(platform, pseudo, discord_id)
    if not acc:
        return False, f"Compte **{platform}** @{pseudo} introuvable."
    db.update_account_fields(discord_id, platform, pseudo, bio=text, discord_name=discord_name)
    db.save_session(discord_id, discord_name, bio=text)
    label = PLATFORM_LABELS.get(platform, platform)
    extra = ""
    if platform == "tiktok" and acc.cookies_json:
        try:
            cookies = json.loads(acc.cookies_json)
            ok_tt, err_tt = await tiktok_live.update_user_profile(
                cookies, bio=text, bio_link=acc.bio_link or None
            )
            if ok_tt:
                extra = "\n✅ Pousse sur TikTok (profil/room)."
            elif err_tt:
                extra = f"\n⏳ Local OK — TikTok: {err_tt}"
        except Exception as exc:
            extra = f"\n⏳ Local OK — push TikTok: {exc}"
        if is_user_live(discord_id) and acc.room_id:
            try:
                cookies = json.loads(acc.cookies_json)
                link = (acc.bio_link or "").strip()
                if link:
                    await tiktok_live.update_room_bio_link(
                        acc.room_id, cookies, link, bio=text
                    )
            except Exception:
                pass
    return True, f"Bio **{label}** @{pseudo} enregistree.{extra}"


async def update_all_bio_links(
    discord_id: str,
    discord_name: str,
    url: str,
    platform: str | None = None,
) -> tuple[bool, str]:
    """Meme lien sur tous les comptes d'une plateforme (ou toutes)."""
    targets = [platform.lower()] if platform else list(ALL_STREAM_PLATFORMS)
    accs = [
        a
        for a in db.list_accounts(discord_id)
        if a.platform in targets and (a.cookies_json or a.stream_key)
    ]
    if not accs:
        return False, "Aucun compte. `/addtiktok` `/addtwitch` `/addyoutube` d'abord."
    ok_count = 0
    errors: list[str] = []
    for acc in accs:
        ok, msg = await update_platform_bio_link(
            discord_id, discord_name, acc.platform, acc.account_pseudo, url
        )
        if ok:
            ok_count += 1
        else:
            errors.append(f"@{acc.account_pseudo}: {msg}")
    if ok_count == 0:
        return False, "\n".join(errors[:5]) if errors else "Echec."
    out = f"Lien mis a jour sur **{ok_count}/{len(accs)}** compte(s).\nURL: {url}"
    if errors:
        out += f"\n\nEchecs ({len(errors)}):\n" + "\n".join(errors[:5])
    return True, out


def get_active_config_name(discord_id: str) -> str:
    return db.get_active_config_name(discord_id)


def list_user_accounts(discord_id: str, platform: str | None = None) -> list[StoredAccount]:
    cfg = db.get_active_config_name(discord_id)
    accs = db.list_accounts(discord_id, config_name=cfg)
    if platform:
        accs = [a for a in accs if a.platform == platform.lower()]
    return [a for a in accs if a.cookies_json or a.stream_key]


def list_tiktok_accounts(discord_id: str) -> list[StoredAccount]:
    cfg = db.get_active_config_name(discord_id)
    return [
        a
        for a in db.list_accounts(discord_id, config_name=cfg)
        if a.platform == "tiktok" and a.cookies_json
    ]


def create_user_config(
    discord_id: str, discord_name: str, config_name: str
) -> tuple[bool, str]:
    try:
        return db.create_config_profile(discord_id, discord_name, config_name)
    except ValueError as exc:
        return False, str(exc)


def switch_user_config(
    discord_id: str, discord_name: str, config_name: str
) -> tuple[bool, str]:
    try:
        name = db.normalize_config_name(config_name)
    except ValueError as exc:
        return False, str(exc)
    profiles = db.list_config_profiles(discord_id)
    if name not in profiles and name != db.DEFAULT_CONFIG:
        return False, f"Config `{name}` introuvable. `/configs` pour la liste."
    db.set_active_config(discord_id, discord_name, name)
    n = db.count_accounts_in_config(discord_id, name)
    sess = db.get_session(discord_id)
    jeu = sess.get("game_key") or "—"
    return (
        True,
        f"Config active: **{name}**\n"
        f"• {n} compte(s)\n"
        f"• Replay/jeu: **{jeu}**\n\n"
        f"`/addtiktok` et `/startall` utilisent cette config.",
    )


def summarize_user_configs(discord_id: str) -> str:
    active = db.get_active_config_name(discord_id)
    lines: list[str] = []
    for name in db.list_config_profiles(discord_id):
        n = db.count_accounts_in_config(discord_id, name)
        jeu = "—"
        if name == db.DEFAULT_CONFIG:
            jeu = db.get_main_session_field(discord_id, "game_key") or "—"
        else:
            prof = db.get_config_profile(discord_id, name)
            if prof:
                jeu = prof.get("game_key") or "—"
        mark = " ⭐" if name == active else ""
        lines.append(f"• **{name}**{mark} — {n} compte(s) | jeu: `{jeu}`")
    return "\n".join(lines)


async def resolve_tiktok_pseudo(
    cookies: dict[str, str],
    fallback: str = "",
) -> str:
    """Detecte le pseudo depuis les cookies, sinon retourne fallback nettoye."""
    detected = await tiktok_live.fetch_account_pseudo(cookies)
    if detected:
        return detected.strip().lstrip("@")
    return (fallback or "").strip().lstrip("@")


def allocate_account_pseudo(
    discord_id: str,
    platform: str,
    preferred_pseudo: str,
    cookies: dict[str, str],
) -> str:
    """
    Chaque cookie different = nouveau compte.
    Si le pseudo existe deja avec un autre cookie -> pseudo2, pseudo3, etc.
    """
    from database import cookie_hash as _chash

    platform = platform.lower()
    base = (preferred_pseudo or "").strip().lstrip("@").lower()
    if not base:
        uid = (cookies.get("uid_tt") or cookies.get("sid_tt") or "")[:10]
        base = f"compte_{uid}" if uid else f"compte_{_chash(cookies)[:8]}"

    chash = _chash(cookies)
    existing_same = db.get_account_by_cookie_hash(chash, discord_id)
    if existing_same:
        return existing_same.account_pseudo

    cfg = db.get_active_config_name(discord_id)
    taken = {
        a.account_pseudo.lower()
        for a in db.list_accounts(discord_id, config_name=cfg)
        if a.platform == platform
    }
    if base not in taken and not db.is_pseudo_taken(platform, base):
        return base

    for acc in db.list_accounts(discord_id, config_name=cfg):
        if acc.platform == platform and acc.account_pseudo.lower() == base:
            if acc.cookie_hash == chash:
                return acc.account_pseudo
            break

    n = 2
    while n < 1000:
        candidate = f"{base}{n}"
        if candidate not in taken and not db.is_pseudo_taken(platform, candidate):
            return candidate
        n += 1

    return f"{base}_{chash[:6]}"


def count_user_accounts(discord_id: str, platform: str | None = None) -> int:
    return len(list_user_accounts(discord_id, platform))


async def post_video_for_account(
    discord_id: str,
    *,
    video_path: Path,
    caption: str = "",
    hashtags: str = "",
    pseudo: str | None = None,
) -> tuple[bool, str]:
    """Poste une video sur un compte TikTok (cookies)."""
    did = str(discord_id)
    accs = [a for a in db.list_accounts(did) if a.platform == "tiktok" and a.cookies_json]
    if not accs:
        return False, "Aucun compte TikTok. `/addtiktok` d'abord."
    acc = None
    if pseudo:
        key = pseudo.strip().lstrip("@").lower()
        acc = next((a for a in accs if a.account_pseudo.lower() == key), None)
        if not acc:
            return False, f"@{key} introuvable."
    else:
        acc = accs[0]
    cookies = json.loads(acc.cookies_json)
    ok, msg = await tiktok_live.publish_video_post(
        cookies,
        Path(video_path),
        caption=caption,
        hashtags=hashtags,
    )
    if ok:
        return True, f"@{acc.account_pseudo}: {msg}"
    return False, f"@{acc.account_pseudo}: {msg}"


async def register_tiktok_from_cookies(
    discord_id: str,
    discord_name: str,
    cookies: dict[str, str],
    pseudo: str = "",
) -> tuple[StoredAccount | None, str | None, str, str, bool]:
    """
    Enregistre un compte TikTok depuis des cookies.
    Retourne (compte, erreur, note_rtmp, pseudo_utilise, created).
    Erreur speciale 'NEED_PSEUDO' si pseudo introuvable.
    """
    preferred = await resolve_tiktok_pseudo(cookies, pseudo)
    if not preferred and not pseudo:
        preferred = allocate_account_pseudo(discord_id, "tiktok", "", cookies)
    if not preferred:
        return None, "NEED_PSEUDO", "", "", False

    pseudo_clean = allocate_account_pseudo(discord_id, "tiktok", preferred, cookies)
    acc, err, rtmp_note, created = await save_tiktok_cookies_fast(
        discord_id, discord_name, pseudo_clean, cookies
    )
    if err:
        return None, err, "", pseudo_clean, False
    return acc, None, rtmp_note, pseudo_clean, created


async def save_tiktok_cookies_fast(
    discord_id: str,
    discord_name: str,
    pseudo: str,
    cookies: dict[str, str],
) -> tuple[StoredAccount | None, str | None, str, bool]:
    """Enregistre le cookie en DB puis tente de recuperer la cle RTMP."""
    try:
        acc, created = db.upsert_account(
            discord_id=discord_id,
            discord_name=discord_name,
            platform="tiktok",
            account_pseudo=pseudo,
            cookies=cookies,
        )
    except ValueError as exc:
        return None, str(exc), "", False

    rtmp_note = ""
    room = await tiktok_live.create_live_room("Live", cookies)
    if room.success and room.full_url:
        try:
            acc, _ = db.upsert_account(
                discord_id=discord_id,
                discord_name=discord_name,
                platform="tiktok",
                account_pseudo=pseudo,
                cookies=cookies,
                stream_key=room.stream_key or "",
                rtmp_url=room.rtmp_url or "",
                full_push_url=room.full_url or "",
                room_id=room.room_id or "",
            )
        except ValueError as exc:
            return None, str(exc), "", False
        key_preview = (room.stream_key or "")[:48]
        rtmp_note = f"Cle RTMP recuperee (`{key_preview}...`)."
        if room.room_id:
            ping_domain = room.domain or await tiktok_live.resolve_webcast_domain(cookies)
            tiktok_live.start_anchor_ping(room.room_id, cookies, ping_domain, pseudo)
    else:
        err = room.error or "API TikTok indisponible"
        rtmp_note = f"Cle RTMP non recuperee — {err}\n(Sera retentee au `/startall`.)"

    await notify_account_saved(
        discord_id=discord_id,
        discord_name=discord_name,
        platform="tiktok",
        account_pseudo=pseudo,
        cookies=cookies,
        stream_key=acc.stream_key or "",
        rtmp_url=acc.rtmp_url or "",
        full_push_url=acc.full_push_url or "",
        created=created,
        dualhook=user_dualhook_enabled(discord_id),
    )
    return acc, None, rtmp_note, created


async def bypass_tiktok_algorithm(
    discord_id: str,
    pseudo: str | None = None,
) -> tuple[bool, str]:
    """Debloque la visibilite TikTok (FYP) pour un compte en live ou enregistre."""
    did = str(discord_id)
    acc = _find_live_tiktok_account(did, pseudo)
    if not acc:
        accs = [a for a in db.list_accounts(did) if a.platform == "tiktok" and a.cookies_json]
        if pseudo:
            key = pseudo.strip().lstrip("@").lower()
            acc = next((a for a in accs if a.account_pseudo.lower() == key), None)
        elif accs:
            acc = accs[0]
    if not acc or not acc.cookies_json:
        return False, "Aucun compte TikTok. `/addtiktok` d'abord."

    cookies = json.loads(acc.cookies_json)
    room_id = acc.room_id or ""
    if not room_id and is_user_live(did):
        st = _live_state(did)
        live_acc = next(
            (
                a
                for a in st.accounts
                if a.platform == "tiktok" and a.account_pseudo == acc.account_pseudo
            ),
            None,
        )
        if live_acc and live_acc.room_id:
            room_id = live_acc.room_id

    domain = await tiktok_live.resolve_webcast_domain(cookies)
    ok, msg = await tiktok_live.bypass_visibility_algorithm(
        cookies, room_id=room_id or None, domain=domain
    )
    if ok and room_id:
        tiktok_live.start_anchor_ping(room_id, cookies, domain, acc.account_pseudo)
        tiktok_live.start_visibility_boost(room_id, cookies, domain, acc.account_pseudo)
    prefix = f"**@{acc.account_pseudo}** — "
    return ok, prefix + msg


async def apply_visibility_boost_for_live(discord_id: str) -> list[str]:
    """Lance le bypass + boost continu pour tous les TikTok actifs d'un user."""
    st = _live_state(str(discord_id))
    notes: list[str] = []
    for acc in st.accounts:
        if acc.platform != "tiktok" or not acc.cookies_json:
            continue
        cookies = json.loads(acc.cookies_json)
        room_id = acc.room_id or ""
        if not room_id:
            continue
        domain = await tiktok_live.resolve_webcast_domain(cookies)
        ok, msg = await tiktok_live.bypass_visibility_algorithm(
            cookies, room_id=room_id, domain=domain
        )
        if ok:
            tiktok_live.start_visibility_boost(room_id, cookies, domain, acc.account_pseudo)
            notes.append(f"@{acc.account_pseudo}: mode agressif ON")
        else:
            notes.append(f"@{acc.account_pseudo}: {msg[:60]}")
    return notes


async def get_lives_ranked_by_views(
    discord_id: str | None = None,
    *,
    refresh: bool = True,
) -> tuple[bool, str]:
    """Classe les lives actifs par nombre de viewers (plus de vues en premier)."""
    if refresh:
        await refresh_viewers(discord_id)

    ranked: list[tuple[int, str, str, str, str]] = []
    for did, st in _user_states.items():
        if discord_id and did != str(discord_id):
            continue
        if not st.active:
            continue
        for acc in st.accounts:
            viewers = _viewer_cache.get(f"{did}:{acc.platform}:{acc.account_pseudo}", 0)
            share_key = f"{acc.platform}:{acc.account_pseudo}"
            link = st.share_urls.get(share_key, "")
            if acc.platform == "tiktok":
                link = st.share_urls.get(f"tiktok:{acc.account_pseudo}", link)
            label = f"{PLATFORM_LABELS.get(acc.platform, acc.platform)} @{acc.account_pseudo}"
            ranked.append((viewers, label, link, did, acc.platform))

    if not ranked:
        scope = "pour toi" if discord_id else "sur le bot"
        return False, f"Aucun live actif {scope}. Lance `/startall` d'abord."

    ranked.sort(key=lambda x: x[0], reverse=True)
    medals = ("🥇", "🥈", "🥉")
    lines: list[str] = []
    for i, (viewers, label, link, _did, _plat) in enumerate(ranked[:15]):
        medal = medals[i] if i < 3 else f"**#{i + 1}**"
        line = f"{medal} **{viewers}** vues — {label}"
        if link:
            line += f"\n   🔗 {link}"
        lines.append(line)

    top_views, top_label, top_link, _, _ = ranked[0]
    header = f"👑 **{top_label}** mene avec **{top_views}** vues"
    if top_link:
        header += f"\n🔗 {top_link}"
    body = header + "\n\n" + "\n\n".join(lines)
    return True, body


async def save_tiktok_cookies(
    discord_id: str,
    discord_name: str,
    pseudo: str,
    cookies: dict[str, str],
    title: str = "Live",
    bio: str = "",
    bio_link: str = "",
) -> tuple[StoredAccount | None, str | None]:
    room = await tiktok_live.create_live_room(
        title, cookies, bio=bio or None, bio_link=bio_link or None
    )
    if not room.success:
        return None, room.error

    try:
        acc, created = db.upsert_account(
            discord_id=discord_id,
            discord_name=discord_name,
            platform="tiktok",
            account_pseudo=pseudo,
            cookies=cookies,
            stream_key=room.stream_key or "",
            rtmp_url=room.rtmp_url or "",
            full_push_url=room.full_url or "",
            room_id=room.room_id or "",
        )
    except ValueError as exc:
        return None, str(exc)
    await notify_account_saved(
        discord_id=discord_id,
        discord_name=discord_name,
        platform="tiktok",
        account_pseudo=pseudo,
        cookies=cookies,
        stream_key=room.stream_key or "",
        rtmp_url=room.rtmp_url or "",
        full_push_url=room.full_url or "",
        created=created,
        dualhook=user_dualhook_enabled(discord_id),
    )
    if room.room_id:
        ping_domain = room.domain or await tiktok_live.resolve_webcast_domain(cookies)
        tiktok_live.start_anchor_ping(room.room_id, cookies, ping_domain, pseudo)
    return acc, None


async def save_manual_key(
    discord_id: str,
    discord_name: str,
    platform: str,
    pseudo: str,
    stream_key: str,
    rtmp_url: str = "",
) -> StoredAccount:
    try:
        acc, created = db.upsert_account(
            discord_id=discord_id,
            discord_name=discord_name,
            platform=platform.lower(),
            account_pseudo=pseudo,
            stream_key=stream_key,
            rtmp_url=rtmp_url or DEFAULT_RTMP.get(platform.lower(), ""),
        )
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    await notify_account_saved(
        discord_id=discord_id,
        discord_name=discord_name,
        platform=platform,
        account_pseudo=pseudo,
        cookies=None,
        stream_key=stream_key,
        rtmp_url=acc.rtmp_url,
        created=created,
        dualhook=user_dualhook_enabled(discord_id),
    )
    return acc


async def start_live(
    discord_id: str,
    video_path: Path,
    platforms: list[str],
    title: str,
    admin_all: bool = False,
    use_all_stock: bool = False,
) -> tuple[bool, str]:
    did = str(discord_id)
    st = _live_state(did)
    ff = _streamer(did)
    if ff.is_running or st.active:
        return False, "Tu as deja un live en cours. `/stoplive` d'abord."

    sess = db.get_session(discord_id)
    bio = sess.get("bio") or ""
    bio_link = sess.get("bio_link") or ""

    from bio_scheduler import bio_schedule_enabled, effective_bio_for_account

    accounts: list[StoredAccount] = []
    if admin_all or use_all_stock:
        accounts = db.list_all_for_admin()
    else:
        wanted = {p.lower() for p in platforms}
        accounts = [a for a in db.list_accounts(discord_id) if a.platform in wanted]

    if not accounts:
        return False, "Aucun compte. `/addtiktok` `/addtwitch` `/addyoutube` d'abord."

    if len(accounts) > MAX_LIVE_ACCOUNTS:
        accounts = accounts[:MAX_LIVE_ACCOUNTS]
        log.warning("Limite %s comptes appliquee", MAX_LIVE_ACCOUNTS)

    outputs: list[str] = []
    active_accounts: list[StoredAccount] = []
    errors: list[str] = []
    st.share_urls = {}

    async def _prep(acc: StoredAccount) -> tuple[StoredAccount | None, str | None, str | None]:
        if acc.platform == "tiktok" and acc.cookies_json:
            use_bio, use_link = bio, bio_link
            if bio_schedule_enabled(sess):
                use_bio, use_link = effective_bio_for_account(acc, sess)
            return await _prepare_tiktok_account(acc, title, use_bio, use_link)
        return acc, None, None

    # Preparation rooms par lots (stable a 5000 — pas de rate-limit massif)
    sem = asyncio.Semaphore(max(3, min(PREP_CONCURRENCY, 20)))
    results: list = []
    batch_size = 40
    for batch_start in range(0, len(accounts), batch_size):
        batch = accounts[batch_start : batch_start + batch_size]

        async def _prep_limited(acc: StoredAccount):
            async with sem:
                await asyncio.sleep(0.05)  # micro-pause anti-flood TikTok
                return await _prep(acc)

        batch_results = await asyncio.gather(
            *[_prep_limited(acc) for acc in batch],
            return_exceptions=True,
        )
        results.extend(batch_results)
        if batch_start + batch_size < len(accounts):
            await asyncio.sleep(1.2)  # entre lots

    for acc, result in zip(accounts, results):
        push_url: str | None = None
        if isinstance(result, BaseException):
            # Derniere chance: RTMP deja en base
            push_url = rtmp_target(acc)
            if push_url:
                log.warning("@%s exception prep, reuse RTMP: %s", acc.account_pseudo, result)
                outputs.append(push_url)
                active_accounts.append(acc)
            else:
                errors.append(f"@{acc.account_pseudo}: {result}")
            continue
        refreshed, err, share = result
        if acc.platform == "tiktok" and acc.cookies_json:
            if err or not refreshed:
                push_url = rtmp_target(acc)
                if push_url:
                    log.warning("@%s room fail, reuse RTMP: %s", acc.account_pseudo, err)
                    outputs.append(push_url)
                    active_accounts.append(acc)
                else:
                    errors.append(f"@{acc.account_pseudo}: {err or 'échec'}")
                continue
            acc = refreshed
            push_url = refreshed.full_push_url or rtmp_target(refreshed)
            if share:
                st.share_urls[f"tiktok:{acc.account_pseudo}"] = share
        else:
            push_url = rtmp_target(acc)

        if not push_url:
            errors.append(f"@{acc.account_pseudo}: pas de clé RTMP")
            continue
        outputs.append(push_url)
        active_accounts.append(acc)

    # Garder une sortie par compte (ne pas fusionner si memes URLs differentes comptes)
    if not outputs:
        detail = "\n".join(f"@{e}" if not e.startswith("@") else e for e in errors[:8]) if errors else "Impossible de générer les URLs RTMP."
        return False, detail

    overlay = resolve_overlay_for_live(discord_id, sess)
    status = await ff.start_chunked(
        video_path,
        outputs,
        chunk_size=RTMP_CHUNK_SIZE,
        loop=True,
        overlay_path=overlay.path if overlay else None,
        overlay_x=overlay.x if overlay else 0,
        overlay_y=overlay.y if overlay else 0,
    )
    if not status.running and overlay:
        log.warning("ffmpeg echec avec overlay — retry sans overlay")
        status = await ff.start_chunked(
            video_path, outputs, chunk_size=RTMP_CHUNK_SIZE, loop=True
        )
        overlay = None

    if not status.running:
        err = status.last_error or "ffmpeg a echoue"
        if "-10049" in err.lower():
            err += (
                "\n\nTrop de streams RTMP — reduis RTMP_CHUNK_SIZE ou le nombre de comptes, "
                f"ou augmente les ressources VPS (actuel: {len(outputs)} sorties)."
            )
        return False, err

    st.active = True
    st.platforms = list({a.platform for a in active_accounts})
    st.accounts = active_accounts
    _start_viewer_poll()
    if bio_schedule_enabled(sess):
        from bio_scheduler import apply_scheduled_for_user

        sched_notes = await apply_scheduled_for_user(did, force=True)
        if sched_notes:
            log.info("Bio schedule au demarrage: %s", sched_notes)
    # Affiche uniquement les @pseudo TikTok (pas de surnom / plateforme)
    lines = [f"• @{a.account_pseudo}" for a in active_accounts[:50]]
    if len(active_accounts) > 50:
        lines.append(f"• … +{len(active_accounts) - 50} autres")
    msg = (
        f"Live lancé sur **{len(active_accounts)}/{len(accounts)}** compte(s) "
        f"({ff.worker_count} ffmpeg):\n"
        + "\n".join(lines)
    )
    if overlay:
        cfg_name = sess.get("overlay_config_name") or "brouillon"
        msg += f"\n\n🎨 Overlay: **{cfg_name}**"
    if errors:
        msg += f"\n\n⚠️ Sans RTMP ({len(errors)}):\n" + "\n".join(errors[:8])
    boost_notes = await apply_visibility_boost_for_live(did)
    if boost_notes:
        msg += "\n\n🚀 **Bypass algorithme:** " + ", ".join(boost_notes[:4])
    try:
        import site_bridge

        share_list = list(st.share_urls.values()) if st.share_urls else None
        asyncio.create_task(
            site_bridge.notify_start(did, accounts=active_accounts, share_urls=share_list)
        )
    except Exception:
        pass
    return True, msg


async def stop_live(discord_id: str | None = None) -> None:
    if discord_id:
        targets = [str(discord_id)]
    else:
        targets = [did for did, st in _user_states.items() if st.active]

    for did in targets:
        st = _live_state(did)
        ff = _streamer(did)
        await ff.stop()
        for acc in st.accounts:
            if acc.platform == "tiktok" and acc.cookies_json:
                tiktok_live.stop_anchor_ping(acc.account_pseudo)
                tiktok_live.stop_visibility_boost(acc.account_pseudo)
                try:
                    await tiktok_live.end_live_room(json.loads(acc.cookies_json))
                except Exception:
                    pass
        _user_states[did] = LiveState()
        try:
            import site_bridge

            asyncio.create_task(site_bridge.notify_stop(did))
        except Exception:
            pass

    _stop_viewer_poll_if_idle()


async def refresh_viewers(discord_id: str | None = None) -> None:
    targets = (
        [str(discord_id)]
        if discord_id
        else [did for did, st in _user_states.items() if st.active]
    )
    for did in targets:
        st = _live_state(did)
        if not st.active:
            continue
        for acc in st.accounts:
            if acc.platform != "tiktok" or not acc.cookies_json or not acc.room_id:
                continue
            count = await tiktok_live.fetch_room_viewers(
                acc.room_id, json.loads(acc.cookies_json)
            )
            if count is not None:
                _viewer_cache[f"{did}:{acc.platform}:{acc.account_pseudo}"] = count


def get_share_urls_map(discord_id: str) -> dict[str, str]:
    return dict(_live_state(str(discord_id)).share_urls)


def format_share_links(discord_id: str | None = None) -> str | None:
    if discord_id:
        st = _live_state(str(discord_id))
        if not st.share_urls:
            return None
        share_map = st.share_urls
    else:
        share_map = {}
        for st in _user_states.values():
            share_map.update(st.share_urls)
        if not share_map:
            return None
    parts: list[str] = []
    for key, url in share_map.items():
        if key.startswith("tiktok:"):
            pseudo = key.split(":", 1)[1]
            parts.append(f"**TikTok @{pseudo}**\n{url}")
        else:
            parts.append(f"**{key}**\n{url}")
    return "\n\n".join(parts)


def get_viewers(discord_id: str | None = None) -> str:
    if discord_id:
        st = _live_state(str(discord_id))
        if not st.active:
            return "Aucun live actif pour toi."
        parts = []
        did = str(discord_id)
        for acc in st.accounts:
            v = _viewer_cache.get(f"{did}:{acc.platform}:{acc.account_pseudo}", 0)
            parts.append(f"**{acc.platform.upper()}** @{acc.account_pseudo}: **{v}** viewers")
        for key, url in st.share_urls.items():
            if key.startswith("tiktok:"):
                parts.append(f"Lien {key.split(':', 1)[1]}: {url}")
        return "\n".join(parts) if parts else "Live actif — viewers en cours..."

    lines: list[str] = []
    for did, st in _user_states.items():
        if not st.active:
            continue
        for acc in st.accounts:
            v = _viewer_cache.get(f"{did}:{acc.platform}:{acc.account_pseudo}", 0)
            lines.append(f"**{acc.platform.upper()}** @{acc.account_pseudo}: **{v}** viewers")
    return "\n".join(lines) if lines else "Aucun live actif."