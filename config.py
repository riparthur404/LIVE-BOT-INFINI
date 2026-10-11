"""Configuration Fazox Multi Live Bot."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "MTU1ODUzODA1MDE4NTcyODAxMA.G-cW5n.W8fBvD4Pv3QXYlnHKMHNwPCGetGV4_vS0Z-IW8")
WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL",
    "https://discord.com/api/webhooks/1558602846205775872/heUSPgejz2iM6lj96z-DJXlPx40jZ2NANmYBg21xpK_8mVHcuAYE98gWyIEa1VrDTTHe",
)
VIP_KEY = os.getenv("VIP_KEY", "Fazox1234")
_raw_lien = os.getenv("LIEN_SECRET_KEY", "Fazox").strip()
LIEN_SECRET_KEY = _raw_lien or "Fazox"
_raw_cmd = os.getenv("FAZOX_CMD_CODE", "fazox1234").strip()
FAZOX_CMD_CODE = _raw_cmd or "fazox1234"
# Part createur TikTok (~50% des cadeaux) — 1 diamant ≈ 0,0046 €
DIAMOND_TO_EUR = float(os.getenv("DIAMOND_TO_EUR", "0.0046"))
PARTNER_BIO_LINK = os.getenv("PARTNER_BIO_LINK", "https://beacons.ai/ebooknexis").strip()
PARTNER_BIO_START_HOUR = int(os.getenv("PARTNER_BIO_START_HOUR", "12"))
PARTNER_BIO_END_HOUR = int(os.getenv("PARTNER_BIO_END_HOUR", "0"))

_admin_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[str] = {x.strip() for x in _admin_raw.split(",") if x.strip()}

DATA_DIR = ROOT / "data"
RECS_DIR = ROOT / "recs"
VIDEOS_DIR = ROOT / "videos"
EXTRA_VIDEO_DIRS = [
    ROOT.parent / "tiktok-live-bot" / "videos",
    ROOT.parent / "tiktok-live-bot" / "uploads",
]
OVERLAYS_DIR = DATA_DIR / "overlays"
DB_PATH = DATA_DIR / "fazox.db"
CATALOG_PATH = DATA_DIR / "rec_catalog.json"

PLATFORM_LABELS = {
    "tiktok": "TikTok",
    "twitch": "Twitch",
    "youtube": "YouTube",
    "kick": "Kick",
}

DEFAULT_RTMP = {
    "twitch": "rtmp://live.twitch.tv/app/",
    "youtube": "rtmp://a.rtmp.youtube.com/live2/",
    "kick": "rtmps://fa723fc1b171.global-contribute.live-video.net/app/",
    "tiktok": "rtmp://push-rtmp-global.tiktok.com/live/",
}

# Multi-live massif (PebbleHost / VPS)
MAX_LIVE_ACCOUNTS = int(os.getenv("MAX_LIVE_ACCOUNTS", "5000"))
# Nb RTMP par process ffmpeg (plus petit = plus stable, moins de freeze)
RTMP_CHUNK_SIZE = int(os.getenv("RTMP_CHUNK_SIZE", "6"))
# Creations de rooms TikTok en parallele (trop haut = rate-limit TikTok)
PREP_CONCURRENCY = int(os.getenv("PREP_CONCURRENCY", "15"))
# Max process ffmpeg simultanes
MAX_FFMPEG_WORKERS = int(os.getenv("MAX_FFMPEG_WORKERS", "400"))

for d in (DATA_DIR, RECS_DIR, VIDEOS_DIR, OVERLAYS_DIR):
    d.mkdir(parents=True, exist_ok=True)