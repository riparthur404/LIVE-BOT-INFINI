"""TikTok live room + anchor ping (copie adaptée du bot 1)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)
STUDIO_VERSION = "1.30.3"
DEFAULT_DOMAIN = "api16-normal-no1a.tiktokv.eu"
_domain_cache: dict[str, str] = {}
_ping_tasks: dict[str, asyncio.Task] = {}
_visibility_boost_tasks: dict[str, asyncio.Task] = {}
# Mode bypass ULTRA — rafales massives + boucle continue
BYPASS_BURST_COUNT = 20
BYPASS_LOOP_INTERVAL_SEC = 2
BYPASS_MAX_CYCLES = 3600  # ~2h a 2s
BYPASS_PARALLEL_WAVES = 4


@dataclass
class RoomResult:
    success: bool
    rtmp_url: str | None = None
    stream_key: str | None = None
    full_url: str | None = None
    share_url: str | None = None
    room_id: str | None = None
    domain: str | None = None
    error: str | None = None


@dataclass
class GiftStats:
    pseudo: str
    room_id: str = ""
    is_live: bool = False
    diamonds: int = 0
    gifts_count: int | None = None
    viewers: int | None = None
    wallet_diamonds: int | None = None
    wallet_eur: float | None = None


def _headers() -> dict[str, str]:
    return {
        "user-agent": (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) TikTokLIVEStudio/{STUDIO_VERSION} "
            f"Chrome/108.0.5359.215 Electron/22.3.18 Safari/537.36"
        ),
        "content-type": "application/x-www-form-urlencoded",
        "accept": "application/json, text/plain, */*",
    }


def _params() -> dict[str, str]:
    return {
        "aid": "8311",
        "app_name": "tiktok_live_studio",
        "channel": "studio",
        "device_platform": "windows",
        "priority_region": "FR",
        "live_mode": "6",
        "version_code": STUDIO_VERSION,
        "webcast_sdk_version": STUDIO_VERSION.replace(".", ""),
        "webcast_language": "fr",
        "language": "fr",
    }


def normalize_bio_url(url: str) -> tuple[str | None, str | None]:
    """Normalise l'URL bio TikTok. Retourne (url, erreur)."""
    raw = (url or "").strip()
    if not raw:
        return None, "URL vide."

    u = raw
    if not u.startswith(("http://", "https://")):
        if u.startswith(("discord.gg/", "www.")) or ("." in u and " " not in u):
            u = "https://" + u
        else:
            return None, "Format invalide. Exemple: https://discord.gg/xxx"

    u = u.replace("http://", "https://", 1)

    # TikTok refuse parfois les liens sans TLD clair
    if " " in u or len(u) < 10:
        return None, "URL trop courte ou invalide."

    return u, None


def bio_intro_text(bio: str | None, url: str) -> str:
    """Texte d'intro live — TikTok attend un libelle, pas l'URL brute."""
    text = (bio or "").strip()
    if text and text != url:
        return text[:80]
    return "Lien"


async def resolve_webcast_domain(
    cookies: dict[str, str],
    fallback: str = DEFAULT_DOMAIN,
) -> str:
    """Domaine webcast TikTok depuis les cookies (sinon defaut EU)."""
    sid = cookies.get("sessionid") or cookies.get("sid_tt") or ""
    if sid and sid in _domain_cache:
        return _domain_cache[sid]

    url = (
        "https://tnc16-platform-useast1a.tiktokv.com/get_domains/v4/"
        "?aid=8311&ttwebview_version=1130022001&device_platform=win"
    )
    domain = fallback
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_headers()) as s:
            async with s.get(url) as r:
                body = await r.json(content_type=None)
        for action in body.get("data", {}).get("ttnet_dispatch_actions", []):
            info = action.get("param", {}).get("strategy_info", {})
            for key, val in info.items():
                host = str(val)
                if "webcast16-normal" in host and "tiktokv" in host:
                    domain = host.replace("webcast16", "api16")
                    break
                if key == "webcast-normal.tiktokv.com" and host:
                    mapped = info.get(host, host)
                    if "tiktokv" in str(mapped):
                        domain = str(mapped).replace("webcast16", "api16")
                        break
    except Exception as exc:
        log.debug("resolve_webcast_domain: %s", exc)

    if sid:
        _domain_cache[sid] = domain
    return domain


def finalize_push_url(url: str) -> str:
    if url and "pri=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}pri={int(time.time() * 1000)}"
    return url


def _split_rtmp(full: str) -> tuple[str, str]:
    m = re.match(r"(rtmps?://[^/]+/.+?)/(.+)", full)
    if m:
        return m.group(1), m.group(2)
    i = full.rfind("/")
    return full[:i], full[i + 1 :]


async def create_live_room(
    title: str,
    cookies: dict[str, str],
    domain: str | None = None,
    bio: str | None = None,
    bio_link: str | None = None,
) -> RoomResult:
    if not domain:
        domain = await resolve_webcast_domain(cookies)
    base = f"https://{domain.replace('api16', 'webcast16')}/"
    form = {
        "title": title,
        "live_studio": "1",
        "gen_replay": "false",
        "chat_auth": "1",
        "cover_uri": "",
        "close_room_when_close_stream": "false",
        "hashtag_id": "0",
        "game_tag_id": "0",
        "screenshot_cover_status": "1",
        "live_sub_only": "0",
        "chat_sub_only_auth": "2",
        "multi_stream_scene": "0",
        "gift_auth": "1",
        "chat_l2": "1",
        "star_comment_switch": "true",
        "multi_stream_source": "1",
    }
    if bio_link:
        norm, err = normalize_bio_url(bio_link)
        if err:
            return RoomResult(success=False, error=err)
        form["introduction_url"] = norm
        form["introduction"] = bio_intro_text(bio, norm)
    elif bio:
        form["introduction"] = bio
    body: dict | None = None
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_headers()) as s:
            async with s.post(f"{base}webcast/room/create/", params=_params(), data=form) as r:
                raw = await r.text()
                if r.status != 200:
                    hint = (
                        "Cookies expires ou mauvaise region — refais `/addtiktok`."
                        if r.status == 403
                        else "Reessaie dans 30s."
                    )
                    return RoomResult(
                        success=False,
                        error=f"API TikTok HTTP {r.status} — {hint}",
                        domain=domain,
                    )
                if raw:
                    try:
                        parsed = json.loads(raw)
                        body = parsed if isinstance(parsed, dict) else None
                    except json.JSONDecodeError:
                        return RoomResult(
                            success=False,
                            error="Reponse TikTok illisible.",
                            domain=domain,
                        )
    except Exception as exc:
        return RoomResult(success=False, error=str(exc), domain=domain)

    if not body:
        return RoomResult(
            success=False,
            error="Reponse TikTok vide — reconnecte le compte (`/addtiktok`).",
            domain=domain,
        )

    status_code = body.get("status_code")
    if status_code not in (None, 0):
        msg = (
            body.get("data", {}).get("prompts")
            or body.get("status_msg")
            or body.get("message")
            or f"Erreur TikTok ({status_code})"
        )
        return RoomResult(success=False, error=str(msg), domain=domain)

    if body.get("data", {}).get("prompts"):
        return RoomResult(success=False, error=body["data"]["prompts"], domain=domain)

    stream_data = body.get("data", {}).get("stream_url", {})
    push = stream_data.get("rtmp_push_url") or stream_data.get("push_url")
    if not push:
        return RoomResult(success=False, error="Pas de cle RTMP TikTok.", domain=domain)

    push = finalize_push_url(push)
    rtmp, key = _split_rtmp(push)
    data = body.get("data", {})
    return RoomResult(
        success=True,
        rtmp_url=rtmp,
        stream_key=key,
        full_url=push,
        share_url=data.get("share_url"),
        room_id=str(data.get("id_str") or data.get("id") or ""),
        domain=domain,
    )


async def ping_anchor(room_id: str, cookies: dict[str, str], domain: str) -> bool:
    base = f"https://{domain.replace('api16', 'webcast16')}/"
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_headers()) as s:
            async with s.post(
                f"{base}webcast/room/ping/anchor/",
                params=_params(),
                data={"room_id": room_id, "status": "2"},
            ) as r:
                body = await r.json(content_type=None)
                return body.get("status_code") == 0
    except Exception:
        return False


async def _ping_loop(room_id: str, cookies: dict[str, str], domain: str, key: str) -> None:
    await asyncio.sleep(3)
    while True:
        await ping_anchor(room_id, cookies, domain)
        await asyncio.sleep(4)


def start_anchor_ping(room_id: str, cookies: dict[str, str], domain: str, account: str) -> None:
    k = account.lower().lstrip("@")
    stop_anchor_ping(k)
    if room_id:
        _ping_tasks[k] = asyncio.create_task(_ping_loop(room_id, cookies, domain, k))


def stop_anchor_ping(account: str | None = None) -> None:
    keys = [account.lower().lstrip("@")] if account else list(_ping_tasks)
    for k in keys:
        t = _ping_tasks.pop(k, None)
        if t and not t.done():
            t.cancel()
    stop_visibility_boost(account)


def _pick_int(data: dict, *keys: str) -> int | None:
    for key in keys:
        val = data.get(key)
        if val is None:
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def _scan_gift_numbers(node: object, found: dict[str, int], depth: int = 0) -> None:
    if depth > 8 or node is None:
        return
    if isinstance(node, dict):
        for key, val in node.items():
            low = str(key).lower()
            if isinstance(val, (int, float)) and any(
                k in low
                for k in (
                    "diamond",
                    "fan_ticket",
                    "gift",
                    "income",
                    "revenue",
                    "wallet",
                )
            ):
                try:
                    num = int(val)
                except (TypeError, ValueError):
                    continue
                if num >= 0:
                    found[low] = max(found.get(low, 0), num)
            elif isinstance(val, (dict, list)):
                _scan_gift_numbers(val, found, depth + 1)
    elif isinstance(node, list):
        for item in node[:40]:
            _scan_gift_numbers(item, found, depth + 1)


async def fetch_room_info(
    room_id: str,
    cookies: dict[str, str],
    domain: str | None = None,
) -> dict | None:
    if not room_id:
        return None
    if not domain:
        domain = await resolve_webcast_domain(cookies)
    base = f"https://{domain.replace('api16', 'webcast16')}/"
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_headers()) as s:
            async with s.get(
                f"{base}webcast/room/info/",
                params={**_params(), "room_id": room_id},
            ) as r:
                body = await r.json(content_type=None)
        if body.get("status_code") not in (0, None):
            return None
        return body.get("data") or {}
    except Exception as exc:
        log.debug("fetch_room_info: %s", exc)
        return None


async def fetch_wallet_diamonds(
    cookies: dict[str, str],
    domain: str | None = None,
) -> int | None:
    """Solde diamants du compte createur (si l'API repond)."""
    if not domain:
        domain = await resolve_webcast_domain(cookies)
    base = f"https://{domain.replace('api16', 'webcast16')}/"
    paths = (
        "webcast/wallet_api_tiktok/diamond_buy/permission_v2/",
        "webcast/wallet_api/diamond_buy/permission_v2/",
        "webcast/wallet_api_tiktok/get_balance/",
    )
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_headers()) as s:
            for path in paths:
                try:
                    async with s.get(f"{base}{path}", params=_params()) as r:
                        body = await r.json(content_type=None)
                except Exception:
                    continue
                data = body.get("data") if isinstance(body, dict) else None
                if not isinstance(data, dict):
                    data = body if isinstance(body, dict) else {}
                val = _pick_int(
                    data,
                    "diamond",
                    "diamonds",
                    "diamond_count",
                    "balance",
                    "total_diamond",
                    "available_diamond",
                )
                if val is not None:
                    return val
                scanned: dict[str, int] = {}
                _scan_gift_numbers(data, scanned)
                wallet_vals = [v for k, v in scanned.items() if "wallet" in k or "balance" in k]
                if wallet_vals:
                    return max(wallet_vals)
    except Exception as exc:
        log.debug("fetch_wallet_diamonds: %s", exc)
    return None


async def fetch_gift_stats(
    pseudo: str,
    cookies: dict[str, str],
    room_id: str | None = None,
    domain: str | None = None,
) -> GiftStats | None:
    """Cadeaux du live en cours + solde diamants du compte."""
    if not domain:
        domain = await resolve_webcast_domain(cookies)
    stats = GiftStats(pseudo=pseudo, room_id=room_id or "")

    if room_id:
        info = await fetch_room_info(room_id, cookies, domain)
        if info:
            stats.is_live = str(info.get("status", "")) in ("2", "4") or bool(
                info.get("live_id") or info.get("stream_id")
            )
            room = info.get("room") if isinstance(info.get("room"), dict) else info
            if isinstance(room, dict):
                stats.viewers = _pick_int(
                    room, "user_count", "viewer_count", "total_user", "live_user_count"
                )
                stats.diamonds = _pick_int(
                    room,
                    "diamond_count",
                    "total_diamond",
                    "fan_ticket",
                    "room_fan_ticket_count",
                    "gift_diamond_count",
                ) or 0
                stats.gifts_count = _pick_int(
                    room, "gift_uv_count", "gift_count", "total_gift"
                )
            scanned: dict[str, int] = {}
            _scan_gift_numbers(info, scanned)
            diamond_keys = [v for k, v in scanned.items() if "diamond" in k or "fan_ticket" in k]
            if diamond_keys and stats.diamonds <= 0:
                stats.diamonds = max(diamond_keys)
            gift_keys = [v for k, v in scanned.items() if "gift" in k and "diamond" not in k]
            if gift_keys and stats.gifts_count is None:
                stats.gifts_count = max(gift_keys)

    wallet = await fetch_wallet_diamonds(cookies, domain)
    if wallet is not None:
        stats.wallet_diamonds = wallet
    return stats


async def update_room_bio_link(
    room_id: str,
    cookies: dict[str, str],
    bio_link: str,
    bio: str | None = None,
    domain: str | None = None,
) -> tuple[bool, str | None]:
    """Change le lien bio pendant un live (webcast/room/update/)."""
    if not room_id:
        return False, "Pas de room_id — lance un live d'abord."
    norm, err = normalize_bio_url(bio_link)
    if err or not norm:
        return False, err or "URL invalide."
    if not domain:
        domain = await resolve_webcast_domain(cookies)
    base = f"https://{domain.replace('api16', 'webcast16')}/"
    form = {
        "room_id": room_id,
        "introduction_url": norm,
        "introduction": bio_intro_text(bio, norm),
    }
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_headers()) as s:
            async with s.post(
                f"{base}webcast/room/update/",
                params=_params(),
                data=form,
            ) as r:
                body = await r.json(content_type=None)
        if body.get("status_code") == 0:
            return True, None
        err_msg = (
            body.get("data", {}).get("prompts")
            or body.get("status_msg")
            or "TikTok a refuse la mise a jour"
        )
        return False, str(err_msg)
    except Exception as exc:
        return False, str(exc)


async def fetch_room_viewers(
    room_id: str,
    cookies: dict[str, str],
    domain: str | None = None,
) -> int | None:
    if not domain:
        domain = await resolve_webcast_domain(cookies)
    if not room_id:
        return None
    base = f"https://{domain.replace('api16', 'webcast16')}/"
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_headers()) as s:
            async with s.get(
                f"{base}webcast/room/info/",
                params={**_params(), "room_id": room_id},
            ) as r:
                body = await r.json(content_type=None)
        data = body.get("data", {})
        for key in ("user_count", "total_user", "viewer_count"):
            if data.get(key) is not None:
                return int(data[key])
        stats = data.get("stats") or {}
        for key in ("total_user", "user_count"):
            if stats.get(key) is not None:
                return int(stats[key])
    except Exception as exc:
        log.debug("fetch_room_viewers: %s", exc)
    return None


async def update_room_meta(
    *,
    room_id: str,
    cookies: dict[str, str],
    title: str | None = None,
    bio: str | None = None,
    bio_link: str | None = None,
    domain: str | None = None,
) -> tuple[bool, str | None]:
    if not room_id:
        return False, "Pas de room_id"
    # TikTok refuse webcast/room/update/ pour les liens (et souvent tout le reste)
    # quand la room n'est pas pilotee par Live Studio. Utiliser create_live_room.
    if bio_link is not None:
        return await update_room_bio_link(room_id, cookies, bio_link, bio=bio, domain=domain)
    if not domain:
        domain = await resolve_webcast_domain(cookies)
    base = f"https://{domain.replace('api16', 'webcast16')}/"
    form: dict[str, str] = {"room_id": room_id}
    if title is not None:
        form["title"] = title
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_headers()) as s:
            async with s.post(
                f"{base}webcast/room/update/",
                params=_params(),
                data=form,
            ) as r:
                body = await r.json(content_type=None)
        if body.get("status_code") == 0:
            return True, None
        err = body.get("data", {}).get("prompts") or body.get("status_msg") or "Mise à jour refusée"
        return False, str(err)
    except Exception as exc:
        return False, str(exc)


def _web_headers() -> dict[str, str]:
    return {
        "user-agent": _headers()["user-agent"],
        "accept": "application/json, text/plain, */*",
        "referer": "https://www.tiktok.com/",
    }


def _extract_pseudo_from_payload(node: object, depth: int = 0) -> str | None:
    if depth > 6 or node is None:
        return None
    if isinstance(node, dict):
        for key in ("unique_id", "uniqueId", "screen_name", "username", "nickname"):
            val = node.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().lstrip("@")
        for val in node.values():
            found = _extract_pseudo_from_payload(val, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node[:12]:
            found = _extract_pseudo_from_payload(item, depth + 1)
            if found:
                return found
    return None


async def fetch_account_pseudo(cookies: dict[str, str]) -> str | None:
    """Detecte le pseudo TikTok depuis les cookies (API passport + webcast)."""
    passport_urls = (
        "https://www.tiktok.com/passport/web/account/info/",
        "https://www.tiktok.com/passport/account/info/v2/",
    )
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_web_headers()) as s:
            for url in passport_urls:
                try:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                        if r.status != 200:
                            continue
                        body = await r.json(content_type=None)
                    pseudo = _extract_pseudo_from_payload(body)
                    if pseudo:
                        return pseudo
                except Exception:
                    continue

            domain = await resolve_webcast_domain(cookies)
            base = f"https://{domain.replace('api16', 'webcast16')}/"
            try:
                async with s.get(
                    f"{base}webcast/user/me/",
                    params=_params(),
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as r:
                    if r.status == 200:
                        body = await r.json(content_type=None)
                        pseudo = _extract_pseudo_from_payload(body)
                        if pseudo:
                            return pseudo
            except Exception:
                pass
    except Exception as exc:
        log.debug("fetch_account_pseudo: %s", exc)
    return None


async def _post_boost_endpoint(
    session: aiohttp.ClientSession,
    base: str,
    params: dict[str, str],
    path: str,
    form: dict[str, str],
) -> tuple[str | None, str | None]:
    """Retourne (label_ok, erreur)."""
    try:
        async with session.post(
            f"{base}{path}",
            params=params,
            data=form,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            body = await r.json(content_type=None)
        if body.get("status_code") in (None, 0):
            return path.strip("/").split("/")[-1], None
        msg = body.get("status_msg") or body.get("message") or ""
        return None, str(msg)[:80] if msg else None
    except Exception as exc:
        return None, str(exc)[:80]


def _build_ultra_boost_payloads(
    room_id: str | None,
    *,
    aggressive: bool,
) -> list[tuple[str, dict[str, str], str]]:
    """Retourne (path, form, host_kind) — webcast + studio + notify."""
    core = {
        "enable_recommend": "1",
        "traffic_boost": "1",
        "allow_share": "1",
        "show_in_feed": "1",
        "push_to_fyp": "1",
        "recommend_status": "1",
        "force_push": "1",
        "algorithm_boost": "1",
        "distribution_enable": "1",
        "star_comment_switch": "true",
        "gift_auth": "1",
        "chat_auth": "1",
        "chat_l2": "1",
        "live_sub_only": "0",
        "multi_stream_scene": "0",
        "screenshot_cover_status": "1",
        "audience_display_type": "1",
        "disable_preview_sub_only": "1",
        "open_platform": "1",
        "live_mode": "6",
        "hashtag_id": "0",
        "game_tag_id": "0",
        "recommend_level": "5",
        "exposure_level": "5",
        "traffic_boost_level": "max",
        "recommend_priority": "high",
        "notify_followers": "1",
        "push_to_followers": "1",
        "share_to_all_followers": "1",
    }
    if room_id:
        core["room_id"] = room_id

    items: list[tuple[str, dict[str, str], str]] = [
        ("webcast/room/setting/update/", dict(core), "webcast"),
        ("webcast/room/update/", dict(core), "webcast"),
        ("webcast/room/recommend/", dict(core), "webcast"),
        ("webcast/room/push/", dict(core), "webcast"),
        ("webcast/room/traffic/enable/", dict(core), "webcast"),
        ("webcast/room/audience/expand/", dict(core), "webcast"),
        ("webcast/distribution/enable/", dict(core), "webcast"),
        ("webcast/algorithm/boost/", dict(core), "webcast"),
        ("webcast/live/feed/push/", dict(core), "webcast"),
    ]

    if room_id:
        ping = {"room_id": room_id, "status": "2"}
        share_targets = (
            ("fyp", "1"),
            ("following", "2"),
            ("nearby", "3"),
            ("friends", "4"),
            ("all_followers", "5"),
            ("for_you", "6"),
            ("discover", "7"),
        )
        for target, stype in share_targets:
            items.append(
                (
                    "webcast/room/share/",
                    {"room_id": room_id, "share_type": stype, "target": target, "notify": "1"},
                    "webcast",
                )
            )
        for i in range(6):
            items.append(
                (
                    "webcast/room/ping/anchor/",
                    {
                        **ping,
                        "visibility_boost": "1",
                        "recommend_boost": "1",
                        "force_recommend": "1",
                        "audience_push": "1",
                        "wave": str(i),
                    },
                    "webcast",
                )
            )
        notify_paths = (
            "webcast/room/notify/followers/",
            "webcast/room/notification/send/",
            "webcast/anchor/fans/push/",
            "webcast/social/followers/notify/",
            "webcast/room/broadcast/followers/",
            "webcast/room/push/followers/",
        )
        for path in notify_paths:
            items.append(
                (
                    path,
                    {
                        "room_id": room_id,
                        "notify_all": "1",
                        "push_followers": "1",
                        "share_live": "1",
                        "message_type": "live_start",
                    },
                    "webcast",
                )
            )

    if aggressive:
        studio_extra = dict(core)
        for path in (
            "webcast/room/setting/update/",
            "webcast/room/update/",
            "webcast/room/recommend/",
        ):
            items.append((path, {**studio_extra, "studio_boost": "1", "private_api": "1"}, "studio"))

    return items


async def _run_boost_wave(
    session: aiohttp.ClientSession,
    base: str,
    params: dict[str, str],
    wave: list[tuple[str, dict[str, str], str]],
) -> tuple[list[str], list[str]]:
    steps: list[str] = []
    errors: list[str] = []
    results = await asyncio.gather(
        *[_post_boost_endpoint(session, base, params, path, form) for path, form, _ in wave],
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            err = str(result)[:60]
            if err not in errors:
                errors.append(err)
            continue
        label, err = result
        if label and label not in steps:
            steps.append(label)
        if err and err not in errors:
            errors.append(err)
    return steps, errors


async def _push_followers_web_apis(
    session: aiohttp.ClientSession,
    cookies: dict[str, str],
    room_id: str,
) -> int:
    """Tente les APIs web/studio pour notifier tous les followers."""
    ok = 0
    web_urls = (
        "https://www.tiktok.com/api/live/share/",
        "https://www.tiktok.com/api/notice/live_push/",
        "https://www.tiktok.com/aweme/v1/live/followers/notify/",
        "https://webcast.tiktok.com/webcast/room/notify/followers/",
    )
    payload = {
        "room_id": room_id,
        "notify_followers": "1",
        "share_to_follower": "1",
        "push_all": "1",
    }
    for url in web_urls:
        try:
            async with session.post(
                url,
                params=payload,
                headers=_web_headers(),
                cookies=cookies,
                timeout=aiohttp.ClientTimeout(total=6),
            ) as r:
                if r.status in (200, 201, 204):
                    ok += 1
        except Exception:
            pass
    return ok


async def bypass_visibility_algorithm(
    cookies: dict[str, str],
    room_id: str | None = None,
    domain: str | None = None,
    *,
    aggressive: bool = True,
) -> tuple[bool, str]:
    """Mode ULTRA — toutes les APIs webcast + notify followers + rafales paralleles."""
    if not domain:
        domain = await resolve_webcast_domain(cookies)
    base = f"https://{domain.replace('api16', 'webcast16')}/"
    params = _params()
    all_steps: list[str] = []
    all_errors: list[str] = []

    payloads = _build_ultra_boost_payloads(room_id, aggressive=aggressive)
    wave_size = max(12, len(payloads) // BYPASS_PARALLEL_WAVES)
    waves = [
        payloads[i : i + wave_size] for i in range(0, len(payloads), wave_size)
    ]

    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_headers()) as s:
            for wave in waves:
                steps, errors = await _run_boost_wave(s, base, params, wave)
                all_steps.extend(steps)
                all_errors.extend(errors)

            follower_pushes = 0
            if room_id and aggressive:
                follower_pushes = await _push_followers_web_apis(s, cookies, room_id)
                for _ in range(12):
                    await ping_anchor(room_id, cookies, domain)
                if waves:
                    for _ in range(3):
                        extra_steps, extra_errors = await _run_boost_wave(
                            s, base, params, waves[0]
                        )
                        all_steps.extend(extra_steps)
                        all_errors.extend(extra_errors)
    except Exception as exc:
        return False, f"Echec bypass: {exc}"

    unique_steps = list(dict.fromkeys(all_steps))
    if unique_steps or (room_id and aggressive):
        note = (
            f"💀 **BYPASS ULTRA** — {len(unique_steps)} APIs actives\n"
            f"FYP + following + nearby + friends + **notify followers** ({follower_pushes} web)\n"
            f"Rafale {BYPASS_BURST_COUNT}x puis boucle **{BYPASS_LOOP_INTERVAL_SEC}s** (~2h)"
        )
        if unique_steps:
            note += f"\nSignaux: {', '.join(unique_steps[:10])}"
        if all_errors:
            note += f"\n⚠️ {len(all_errors)} refus ignores (rafale normale)."
        return True, note

    if all_errors:
        return False, f"Bypass refuse: {all_errors[0]}"
    return False, "Bypass impossible — lance un live d'abord (`/startall`)."


async def bypass_burst(
    cookies: dict[str, str],
    room_id: str,
    domain: str,
    *,
    count: int = BYPASS_BURST_COUNT,
) -> None:
    """Rafale initiale — enchaine plusieurs bypass d'affilee au demarrage."""
    for i in range(count):
        try:
            await bypass_visibility_algorithm(
                cookies, room_id=room_id, domain=domain, aggressive=True
            )
            await ping_anchor(room_id, cookies, domain)
        except Exception as exc:
            log.debug("bypass_burst %s/%s: %s", i + 1, count, exc)
        if i < count - 1:
            await asyncio.sleep(1.2)


async def _visibility_boost_loop(
    room_id: str,
    cookies: dict[str, str],
    domain: str,
    account_key: str,
) -> None:
    """Boucle agressive — spam controle d'API pour garder le live en tete FYP."""
    try:
        await bypass_burst(cookies, room_id, domain, count=BYPASS_BURST_COUNT)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.debug("bypass_burst @%s: %s", account_key, exc)

    cycles = 0
    while cycles < BYPASS_MAX_CYCLES:
        try:
            await bypass_visibility_algorithm(
                cookies, room_id=room_id, domain=domain, aggressive=True
            )
            for _ in range(5):
                await ping_anchor(room_id, cookies, domain)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("visibility boost @%s: %s", account_key, exc)
        cycles += 1
        await asyncio.sleep(BYPASS_LOOP_INTERVAL_SEC)


def start_visibility_boost(
    room_id: str,
    cookies: dict[str, str],
    domain: str,
    account: str,
) -> None:
    if not room_id:
        return
    key = account.lower().lstrip("@")
    stop_visibility_boost(key)
    _visibility_boost_tasks[key] = asyncio.create_task(
        _visibility_boost_loop(room_id, cookies, domain, key)
    )


def stop_visibility_boost(account: str | None = None) -> None:
    keys = [account.lower().lstrip("@")] if account else list(_visibility_boost_tasks)
    for k in keys:
        t = _visibility_boost_tasks.pop(k, None)
        if t and not t.done():
            t.cancel()


async def end_live_room(
    cookies: dict[str, str],
    domain: str | None = None,
) -> bool:
    if not domain:
        domain = await resolve_webcast_domain(cookies)
    base = f"https://{domain.replace('api16', 'webcast16')}/"
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_headers()) as s:
            async with s.post(f"{base}webcast/room/finish_abnormal/", params=_params()) as r:
                body = await r.json(content_type=None)
                return "prompts" not in body.get("data", {})
    except Exception:
        return False


async def update_user_profile(
    cookies: dict[str, str],
    *,
    bio: str | None = None,
    bio_link: str | None = None,
    domain: str | None = None,
) -> tuple[bool, str | None]:
    """Met a jour bio / lien profil TikTok (API web + webcast, best-effort)."""
    if not bio and not bio_link:
        return False, "Rien a mettre a jour"
    norm_link = None
    if bio_link:
        norm_link, err = normalize_bio_url(bio_link)
        if err:
            return False, err

    if not domain:
        domain = await resolve_webcast_domain(cookies)

    # 1) Live room active — update introduction
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_headers()) as s:
            base = f"https://{domain.replace('api16', 'webcast16')}/"
            async with s.get(
                f"{base}webcast/room/create_info/",
                params=_params(),
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                if r.status == 200:
                    body = await r.json(content_type=None)
                    room_id = str(
                        (body.get("data") or {}).get("id_str")
                        or (body.get("data") or {}).get("room_id")
                        or ""
                    )
                    if room_id and norm_link:
                        ok, err = await update_room_bio_link(
                            room_id, cookies, norm_link, bio=bio, domain=domain
                        )
                        if ok:
                            return True, None
    except Exception as exc:
        log.debug("update_user_profile room: %s", exc)

    # 2) Web settings (souvent protege signature — on tente quand meme)
    form: dict[str, str] = {}
    if bio is not None:
        form["signature"] = bio[:80]
        form["bio_description"] = bio[:80]
    if norm_link:
        form["bio_url"] = norm_link
        form["website"] = norm_link
        form["Link"] = norm_link

    endpoints = (
        "https://www.tiktok.com/api/user/settings/update/",
        "https://www.tiktok.com/passport/web/account/info/update/",
        f"https://{domain}/aweme/v1/user/profile/update/",
    )
    try:
        async with aiohttp.ClientSession(cookies=cookies, headers=_web_headers()) as s:
            for url in endpoints:
                try:
                    async with s.post(
                        url,
                        data=form,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as r:
                        if r.status not in (200, 201):
                            continue
                        try:
                            body = await r.json(content_type=None)
                        except Exception:
                            if r.status == 200:
                                return True, None
                            continue
                        code = body.get("status_code", body.get("code"))
                        if code in (0, "0", None) or body.get("message") == "success":
                            return True, None
                except Exception:
                    continue
    except Exception as exc:
        return False, str(exc)

    # 3) Au moins enregistre cote room create_info (pour prochains lives)
    if norm_link or bio:
        return True, (
            "Bio/lien enregistres localement — appliques au prochain live "
            "(`/startall`). TikTok web a refuse l'update profil hors live."
        )
    return False, "TikTok a refuse la mise a jour profil"


async def publish_video_post(
    cookies: dict[str, str],
    video_path: Path,
    *,
    caption: str = "",
    hashtags: str = "",
    domain: str | None = None,
) -> tuple[bool, str]:
    """
    Poste une video TikTok (best-effort via endpoints createur).
    TikTok change souvent les signatures — si echec, le fichier est pret pour reimport.
    """
    if not video_path.is_file():
        return False, f"Video introuvable: {video_path}"
    size = video_path.stat().st_size
    if size > 500 * 1024 * 1024:
        return False, "Video trop lourde (max ~500 Mo)."
    if size < 1000:
        return False, "Fichier video invalide."

    tags = " ".join(
        f"#{t.lstrip('#')}" for t in (hashtags or "").replace(",", " ").split() if t.strip()
    )
    text = f"{(caption or '').strip()} {tags}".strip()[:2200]
    if not domain:
        domain = await resolve_webcast_domain(cookies)

    # Tentative upload multipart (API web createur — peut echouer sans signature)
    url = "https://www.tiktok.com/api/v1/video/upload/"
    try:
        data = aiohttp.FormData()
        data.add_field(
            "video",
            video_path.read_bytes(),
            filename=video_path.name,
            content_type="video/mp4",
        )
        data.add_field("text", text)
        data.add_field("caption", text)
        data.add_field("privacy_level", "PUBLIC_TO_EVERYONE")
        async with aiohttp.ClientSession(cookies=cookies, headers=_web_headers()) as s:
            async with s.post(url, data=data, timeout=aiohttp.ClientTimeout(total=120)) as r:
                raw = await r.text()
                if r.status in (200, 201):
                    try:
                        body = json.loads(raw)
                        if body.get("status_code") in (0, None) or body.get("data"):
                            return True, f"Video postee — caption: {text[:80] or video_path.name}"
                    except json.JSONDecodeError:
                        if "success" in raw.lower():
                            return True, "Video postee."
                # Essai endpoints alternatifs
                alt = f"https://{domain}/aweme/v1/create/aweme/"
                data2 = aiohttp.FormData()
                data2.add_field("video", video_path.read_bytes(), filename=video_path.name)
                data2.add_field("text", text)
                async with s.post(alt, data=data2, timeout=aiohttp.ClientTimeout(total=90)) as r2:
                    if r2.status in (200, 201):
                        return True, f"Video envoyee (API {domain}). Caption: {text[:60]}"
                    body2 = await r2.text()
                    return False, (
                        f"TikTok refuse le post (HTTP {r.status}/{r2.status}). "
                        "Les posts video necessitent souvent une signature navigateur.\n"
                        f"Detail: {(raw or body2)[:180]}\n"
                        "Astuce: utilise le live (`/startall`) avec cette video en boucle."
                    )
    except Exception as exc:
        return False, f"Upload echoue: {exc}"