#!/usr/bin/env python3
"""
TikTok Account Unlock Tool — debloquer visibilite / shadow restriction via APIs.

Usage:
  python tiktok_shadow_unlock.py --cookies cookies.json
  python tiktok_shadow_unlock.py --cookies cookies.txt --burst 30 --loop 120
  python tiktok_shadow_unlock.py --cookies cookies.json --check-only

IMPORTANT:
  TikTok n'a PAS d'API publique "supprimer shadowban".
  Ce tool envoie toutes les requetes connues (webcast + web + passport)
  pour restaurer visibilite, FYP, followers notify et sante du compte.
  Resultat depend du compte — cookies valides requis (sessionid/sid_tt ideal).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cookie_parser import parse_cookies

try:
    import aiohttp
    import tiktok_live
except ImportError as exc:
    print("ERREUR: pip install aiohttp")
    raise SystemExit(1) from exc


def _load_cookies(path: Path) -> dict[str, str]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    parsed = parse_cookies(raw)
    if not parsed.success or not parsed.cookies:
        raise ValueError(parsed.error or "Cookies invalides")
    if parsed.warning:
        print(f"[!] {parsed.warning}")
    return parsed.cookies


async def _post(
    session: aiohttp.ClientSession,
    url: str,
    *,
    data: dict | None = None,
    params: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, dict | str | None]:
    try:
        async with session.post(
            url,
            data=data,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=12),
        ) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = await r.text()
            return r.status, body
    except Exception as exc:
        return 0, str(exc)


async def _get(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, dict | str | None]:
    try:
        async with session.get(
            url,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=12),
        ) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = await r.text()
            return r.status, body
    except Exception as exc:
        return 0, str(exc)


async def check_account_health(cookies: dict[str, str]) -> dict:
    """Verifie session, pseudo, capacite live."""
    report: dict = {
        "pseudo": None,
        "session_ok": False,
        "can_create_room": False,
        "room_id": None,
        "errors": [],
        "hints": [],
    }

    pseudo = await tiktok_live.fetch_account_pseudo(cookies)
    report["pseudo"] = pseudo

    if not pseudo:
        report["errors"].append("Pseudo introuvable — cookies incomplets?")
        report["hints"].append("Exporte les cookies avec sessionid ou sid_tt connecte.")
    else:
        report["session_ok"] = True

    domain = await tiktok_live.resolve_webcast_domain(cookies)
    report["domain"] = domain

    room = await tiktok_live.create_live_room("Test unlock", cookies, domain=domain)
    if room.success:
        report["can_create_room"] = True
        report["room_id"] = room.room_id
        report["hints"].append("Compte peut creer une room live — bon signe.")
        if room.room_id:
            await tiktok_live.end_live_room(cookies, domain)
    else:
        err = room.error or "inconnu"
        report["errors"].append(f"Room live: {err}")
        if "403" in err or "expire" in err.lower():
            report["hints"].append("Cookies expires — reconnecte le compte TikTok.")
        if "shadow" in err.lower() or "restrict" in err.lower():
            report["hints"].append("Restriction detectee — lance le bypass ci-dessous.")

    return report


async def run_shadow_unlock(
    cookies: dict[str, str],
    *,
    burst: int = 15,
    loop_seconds: int = 60,
    interval: float = 2.0,
) -> dict:
    """
    Rafale d'APIs pour debloquer visibilite / restrictions algorithmiques.
    """
    stats = {
        "pseudo": await tiktok_live.fetch_account_pseudo(cookies),
        "api_ok": 0,
        "api_fail": 0,
        "waves": 0,
        "room_id": None,
        "messages": [],
    }

    domain = await tiktok_live.resolve_webcast_domain(cookies)
    base = f"https://{domain.replace('api16', 'webcast16')}/"
    params = tiktok_live._params()
    web_h = tiktok_live._web_headers()
    studio_h = tiktok_live._headers()

    # 1) Sante compte — passport + user/me
    passport_urls = (
        "https://www.tiktok.com/passport/web/account/info/",
        "https://www.tiktok.com/passport/account/info/v2/",
    )
    async with aiohttp.ClientSession(cookies=cookies, headers=web_h) as s:
        for url in passport_urls:
            code, body = await _get(s, url)
            if code == 200:
                stats["api_ok"] += 1
                stats["messages"].append(f"OK passport: {url.split('/')[-2]}")
            else:
                stats["api_fail"] += 1

        code, _ = await _get(
            s,
            f"{base}webcast/user/me/",
            params=params,
        )
        if code == 200:
            stats["api_ok"] += 1
        else:
            stats["api_fail"] += 1

    # 2) Creer room temporaire pour unlock (puis bypass dessus)
    room = await tiktok_live.create_live_room("Live", cookies, domain=domain)
    room_id = room.room_id if room.success else None
    stats["room_id"] = room_id

    if not room_id:
        stats["messages"].append(f"Pas de room: {room.error or '—'}")
        stats["messages"].append("Bypass partiel sans room_id...")
    else:
        stats["messages"].append(f"Room {room_id} — bypass complet actif.")

    # 3) Rafale bypass ULTRA (bot fazox)
    for i in range(burst):
        ok, msg = await tiktok_live.bypass_visibility_algorithm(
            cookies, room_id=room_id, domain=domain, aggressive=True
        )
        stats["waves"] += 1
        if ok:
            stats["api_ok"] += 1
        else:
            stats["api_fail"] += 1
        if room_id:
            await tiktok_live.ping_anchor(room_id, cookies, domain)
        if i < burst - 1:
            await asyncio.sleep(0.8)

    # 4) APIs web supplementaires — followers / distribution
    web_unlock = (
        ("https://www.tiktok.com/api/live/share/", {"share_all": "1", "notify": "1"}),
        ("https://www.tiktok.com/api/notice/live_push/", {"push_followers": "1"}),
        ("https://www.tiktok.com/aweme/v1/user/profile/self/", {}),
        ("https://www.tiktok.com/tiktok/v1/algorithm/appeal/status/", {}),
    )
    if room_id:
        web_unlock = (
            *web_unlock,
            (f"https://webcast.tiktok.com/webcast/room/notify/followers/", {"room_id": room_id}),
        )

    async with aiohttp.ClientSession(cookies=cookies, headers=web_h) as s:
        for url, extra in web_unlock:
            code, _ = await _post(s, url, data=extra if extra else None)
            if code in (200, 201, 204):
                stats["api_ok"] += 1
            else:
                stats["api_fail"] += 1

    # 5) Boucle maintain si demandee
    if loop_seconds > 0 and room_id:
        deadline = time.time() + loop_seconds
        cycles = 0
        while time.time() < deadline:
            await tiktok_live.bypass_visibility_algorithm(
                cookies, room_id=room_id, domain=domain, aggressive=True
            )
            await tiktok_live.ping_anchor(room_id, cookies, domain)
            cycles += 1
            stats["waves"] += 1
            await asyncio.sleep(interval)
        stats["messages"].append(f"Boucle maintain: {cycles} cycles ({interval}s)")

    # 6) Nettoyage room test
    if room_id:
        await tiktok_live.end_live_room(cookies, domain)
        tiktok_live.stop_anchor_ping(stats["pseudo"] or "unlock")
        tiktok_live.stop_visibility_boost(stats["pseudo"] or "unlock")

    return stats


def _print_report(title: str, data: dict) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)
    for k, v in data.items():
        if k == "messages" and isinstance(v, list):
            print("messages:")
            for m in v:
                print(f"  • {m}")
        elif k == "hints" and isinstance(v, list):
            if v:
                print("conseils:")
                for h in v:
                    print(f"  → {h}")
        elif k == "errors" and isinstance(v, list):
            if v:
                print("erreurs:")
                for e in v:
                    print(f"  ✗ {e}")
        else:
            print(f"{k}: {v}")


async def main_async(args: argparse.Namespace) -> int:
    cookies = _load_cookies(Path(args.cookies))
    print(f"[+] {len(cookies)} cookies charges")

    health = await check_account_health(cookies)
    _print_report("DIAGNOSTIC COMPTE", health)

    if args.check_only:
        return 0 if health.get("session_ok") else 1

    print()
    print("[*] Lancement unlock / bypass algorithme...")
    stats = await run_shadow_unlock(
        cookies,
        burst=args.burst,
        loop_seconds=args.loop,
        interval=args.interval,
    )
    _print_report("RESULTAT UNLOCK", stats)

    print()
    if stats["api_ok"] > stats["api_fail"]:
        print("✅ Bypass termine — verifie ton compte (FYP, live, vues) dans 15-30 min.")
        print("   Si toujours limite: cookies frais + contenu original + pas de spam.")
    else:
        print("⚠️ Beaucoup de refus API — renouvelle les cookies ou compte trop restreint.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="TikTok shadow unlock — bypass visibilite via APIs (cookies requis)",
    )
    p.add_argument(
        "--cookies", "-c",
        required=True,
        help="Fichier cookies (.json, .txt, Netscape, header)",
    )
    p.add_argument(
        "--burst", "-b",
        type=int,
        default=15,
        help="Nombre de rafales bypass (defaut 15)",
    )
    p.add_argument(
        "--loop", "-l",
        type=int,
        default=60,
        help="Secondes de boucle maintain (0 = desactive, defaut 60)",
    )
    p.add_argument(
        "--interval", "-i",
        type=float,
        default=2.0,
        help="Intervalle boucle en secondes (defaut 2)",
    )
    p.add_argument(
        "--check-only",
        action="store_true",
        help="Diagnostic seulement, pas de bypass",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()