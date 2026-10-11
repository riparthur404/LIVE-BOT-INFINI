"""Auto live multi-plateforme — relance TikTok / YouTube (et Twitch) en boucle."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import discord

from database import Database
from live_engine import start_live, stop_live
from rec_library import resolve_video_path

log = logging.getLogger(__name__)

MIN_INTERVAL_MINUTES = 4
MAX_INTERVAL_MINUTES = 30
DEFAULT_INTERVAL_MINUTES = 14
MAX_LAUNCH_RETRIES = 5

db = Database()


def clamp_minutes(minutes: int) -> int:
    return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, int(minutes)))


@dataclass
class AutoLiveJob:
    discord_id: str
    platforms: list[str]
    channel_id: int
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    task: Optional[asyncio.Task] = None
    active: bool = True
    cycle: int = 0


class AutoLiveManager:
    def __init__(self) -> None:
        self._jobs: dict[str, AutoLiveJob] = {}

    def is_active(self, discord_id: str) -> bool:
        job = self._jobs.get(str(discord_id))
        return bool(job and job.active)

    def get_job(self, discord_id: str) -> Optional[AutoLiveJob]:
        job = self._jobs.get(str(discord_id))
        return job if job and job.active else None

    async def start(
        self,
        bot: discord.Client,
        discord_id: str,
        discord_name: str,
        channel_id: int,
        platforms: list[str],
        interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    ) -> Optional[str]:
        did = str(discord_id)
        if self.is_active(did):
            labels = ", ".join(platforms)
            return f"Auto live deja actif ({labels}). `/stopautolive` d'abord."

        wanted = [p.lower() for p in platforms if p]
        if not wanted:
            return "Aucune plateforme selectionnee."

        accounts = [
            a
            for a in db.list_accounts(did)
            if a.platform in wanted and (a.cookies_json or a.stream_key)
        ]
        if not accounts:
            return "Aucun compte pour cette plateforme. `/addtiktok` `/addyoutube` etc."

        vpath = resolve_video_path(db.get_session(did).get("video_path"))
        if not vpath:
            return "Aucune video. `/jeu nom:0705` d'abord."

        interval = clamp_minutes(interval_minutes)
        db.save_session(
            did,
            discord_name,
            platforms=wanted,
            autolive_minutes=interval,
            autolive_platforms=wanted,
        )

        job = AutoLiveJob(
            discord_id=did,
            platforms=wanted,
            channel_id=channel_id,
            interval_minutes=interval,
        )
        self._jobs[did] = job
        job.task = asyncio.create_task(
            self._loop(bot, job, discord_name),
            name=f"autolive-{did}",
        )
        return None

    async def stop_all(self) -> int:
        """Arrete tous les auto live actifs (ex: au demarrage du bot)."""
        stopped = 0
        for did in list(self._jobs.keys()):
            await self.stop(did)
            stopped += 1
        return stopped

    async def stop(self, discord_id: str) -> list[str]:
        did = str(discord_id)
        job = self._jobs.get(did)
        notes: list[str] = []

        if job:
            job.active = False
            if job.task and not job.task.done():
                job.task.cancel()
            self._jobs.pop(did, None)
            notes.append(
                f"Auto live arrete ({', '.join(job.platforms)}, {job.interval_minutes} min)"
            )

        await stop_live(did)
        if not notes:
            notes.append("Aucun auto live actif.")
        return notes

    async def _loop(
        self,
        bot: discord.Client,
        job: AutoLiveJob,
        discord_name: str,
    ) -> None:
        channel = bot.get_channel(job.channel_id)
        interval_sec = job.interval_minutes * 60

        try:
            while job.active:
                sess = db.get_session(job.discord_id)
                vpath = resolve_video_path(sess.get("video_path"))
                title = sess.get("live_title") or "Live"

                if not vpath:
                    if channel:
                        try:
                            await channel.send(
                                "❌ **Auto live arrete** — video introuvable. `/jeu` puis relance."
                            )
                        except discord.HTTPException:
                            pass
                    break

                if job.cycle == 0:
                    head = (
                        f"🔁 **Auto live** — {', '.join(job.platforms)} "
                        f"(toutes les {job.interval_minutes} min)"
                    )
                else:
                    head = (
                        f"🔁 **Auto live** — relance cycle {job.cycle + 1} "
                        f"({', '.join(job.platforms)})"
                    )
                if channel:
                    try:
                        await channel.send(head)
                    except discord.HTTPException as exc:
                        log.warning("Auto live message: %s", exc)

                await stop_live(job.discord_id)
                await asyncio.sleep(3)

                ok = False
                msg = "Erreur inconnue"
                for attempt in range(MAX_LAUNCH_RETRIES):
                    if attempt > 0:
                        await asyncio.sleep(5)
                        if channel:
                            try:
                                await channel.send(
                                    f"🔄 Nouvelle tentative ({attempt + 1}/{MAX_LAUNCH_RETRIES})..."
                                )
                            except discord.HTTPException:
                                pass
                    ok, msg = await start_live(
                        job.discord_id,
                        vpath,
                        job.platforms,
                        title,
                    )
                    if ok:
                        break
                    log.warning(
                        "Auto live echec %s (tentative %s/%s): %s",
                        job.discord_id,
                        attempt + 1,
                        MAX_LAUNCH_RETRIES,
                        msg,
                    )

                if not ok:
                    if channel:
                        try:
                            await channel.send(
                                f"❌ **Auto live arrete** — {msg}\n`/stopautolive` puis corrige et relance."
                            )
                        except discord.HTTPException:
                            pass
                    break

                if channel:
                    try:
                        await channel.send(f"✅ Live lance (cycle {job.cycle + 1})\n{msg[:1500]}")
                    except discord.HTTPException:
                        pass

                if not await self._wait(job, interval_sec):
                    break

                if channel:
                    try:
                        await channel.send("⏹️ Fin du cycle — relance...")
                    except discord.HTTPException:
                        pass

                await stop_live(job.discord_id)
                job.cycle += 1
                await asyncio.sleep(8)
        except asyncio.CancelledError:
            log.info("Auto live annule pour %s", job.discord_id)
        except Exception:
            log.exception("Auto live crash %s", job.discord_id)
            if channel:
                try:
                    await channel.send("❌ **Auto live crash** — mode arrete.")
                except discord.HTTPException:
                    pass
        finally:
            self._jobs.pop(job.discord_id, None)
            await stop_live(job.discord_id)

    async def _wait(self, job: AutoLiveJob, seconds: int) -> bool:
        for _ in range(seconds):
            if not job.active:
                return False
            await asyncio.sleep(1)
        return job.active


autolive = AutoLiveManager()