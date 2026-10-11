"""ffmpeg — vidéo en boucle fluide, multi-RTMP stable (mass live)."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_FPS_SYNC_ARGS: list[str] | None = None


def _fps_sync_args() -> list[str]:
    """-fps_mode requiert ffmpeg 5.1+ ; PebbleHost a souvent ffmpeg 4.x."""
    global _FPS_SYNC_ARGS
    if _FPS_SYNC_ARGS is not None:
        return _FPS_SYNC_ARGS
    try:
        proc = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=8,
            errors="replace",
        )
        match = re.search(r"ffmpeg version (\d+)(?:\.(\d+))?", proc.stdout or proc.stderr or "")
        if match:
            major = int(match.group(1))
            minor = int(match.group(2) or 0)
            if major > 5 or (major == 5 and minor >= 1):
                _FPS_SYNC_ARGS = ["-fps_mode", "cfr"]
                return _FPS_SYNC_ARGS
    except Exception as exc:
        log.debug("Detection ffmpeg: %s", exc)
    _FPS_SYNC_ARGS = ["-vsync", "cfr"]
    return _FPS_SYNC_ARGS


@dataclass
class StreamJob:
    video_path: Path
    outputs: list[str]
    loop: bool = True
    overlay_path: Path | None = None
    overlay_x: int = 0
    overlay_y: int = 0
    # Mass-live: encode leger pour ne pas freez le CPU
    mass_mode: bool = False


@dataclass
class StreamStatus:
    running: bool = False
    pid: Optional[int] = None
    started_at: Optional[float] = None
    job: Optional[StreamJob] = None
    last_error: Optional[str] = None
    log_lines: list[str] = field(default_factory=list)


def _base_video_filter(*, fps: int = 30) -> str:
    # setpts lisse les trous de timestamp (cause principale des freezes)
    return (
        f"fps={fps},setpts=PTS-STARTPTS,"
        "scale=720:1280:force_original_aspect_ratio=decrease,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=0x1a1a2e,"
        "format=yuv420p"
    )


def _video_has_audio(video_path: Path) -> bool:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            errors="replace",
        )
        return "audio" in (proc.stdout or "")
    except Exception:
        return True


def build_ffmpeg_command(job: StreamJob) -> list[str]:
    mass = job.mass_mode or len(job.outputs) > 3
    fps = 30
    preset = "ultrafast" if mass else "veryfast"
    bitrate = "1200k" if mass else "2000k"
    maxrate = "1500k" if mass else "2300k"
    bufsize = "3000k" if mass else "4600k"
    threads = "1" if mass else "0"
    audio_br = "96k" if mass else "128k"

    base_vf = _base_video_filter(fps=fps)
    has_overlay = bool(job.overlay_path and job.overlay_path.is_file())

    # Ordre important: stream_loop AVANT -i, -re pour cadence live
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-threads",
        threads,
        "-fflags",
        "+genpts+discardcorrupt+igndts",
        "-avoid_negative_ts",
        "make_zero",
        "-thread_queue_size",
        "512",
    ]
    if job.loop:
        # Apres ensure_looped_video, le fichier est long → moins de seek = moins de pause
        cmd.extend(["-stream_loop", "-1"])
    cmd.extend(
        [
            "-re",
            "-i",
            str(job.video_path),
        ]
    )
    if has_overlay:
        cmd.extend(["-loop", "1", "-framerate", str(fps), "-i", str(job.overlay_path)])

    has_audio = _video_has_audio(job.video_path)
    silent_idx: int | None = None
    if not has_audio:
        silent_idx = 2 if has_overlay else 1
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
            ]
        )

    if has_overlay:
        ox, oy = max(0, job.overlay_x), max(0, job.overlay_y)
        filter_complex = (
            f"[0:v]{base_vf}[base];"
            f"[1:v]format=rgba,scale=iw:ih[ov];"
            f"[base][ov]overlay={ox}:{oy}:format=auto:shortest=0[outv]"
        )
        cmd.extend(["-filter_complex", filter_complex, "-map", "[outv]"])
        if has_audio:
            cmd.extend(["-map", "0:a:0?"])
        elif silent_idx is not None:
            cmd.extend(["-map", f"{silent_idx}:a:0"])
    else:
        cmd.extend(["-map", "0:v:0", "-vf", base_vf])
        if has_audio:
            cmd.extend(["-map", "0:a:0?"])
        elif silent_idx is not None:
            cmd.extend(["-map", f"{silent_idx}:a:0"])

    cmd.extend(_fps_sync_args())
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-tune",
            "zerolatency",
            "-profile:v",
            "baseline",
            "-level",
            "3.1",
            "-b:v",
            bitrate,
            "-maxrate",
            maxrate,
            "-bufsize",
            bufsize,
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(fps * 2),
            "-keyint_min",
            str(fps),
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            audio_br,
            "-ar",
            "44100",
            "-ac",
            "2",
            # Lisse l'audio sans couper (pas de -shortest = pas de restart/pause)
            "-af",
            "aresample=async=1000:first_pts=0",
            "-max_muxing_queue_size",
            "1024",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-flvflags",
            "no_duration_filesize",
        ]
    )

    if len(job.outputs) == 1:
        cmd.extend(
            [
                "-f",
                "flv",
                "-rtmp_buffer",
                "1000",
                job.outputs[0],
            ]
        )
    else:
        # onfail=ignore: un RTMP mort ne freeze pas les autres
        tee = "|".join(f"[f=flv:onfail=ignore]{u}" for u in job.outputs)
        cmd.extend(["-f", "tee", tee])
    return cmd


def _short_ffmpeg_error(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "ffmpeg a quitte sans message — verifie que ffmpeg est installe."
    for line in reversed(text.splitlines()):
        low = line.lower()
        if any(k in low for k in ("error", "invalid", "failed", "no such", "not found")):
            return line.strip()[:400]
    return text[-400:]


class FfmpegStreamer:
    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self.status = StreamStatus()
        self._keep_alive = False
        self._watchdog: Optional[asyncio.Task] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._restart_count = 0
        self._last_restart = 0.0

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    async def _read_stderr(self) -> None:
        if not self._process or not self._process.stderr:
            return
        loop = asyncio.get_event_loop()
        while self.is_running:
            line = await loop.run_in_executor(None, self._process.stderr.readline)
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded:
                self.status.log_lines.append(decoded)
                if len(self.status.log_lines) > 40:
                    self.status.log_lines.pop(0)

    async def _collect_stderr(self) -> str:
        if self._reader_task and not self._reader_task.done():
            try:
                await asyncio.wait_for(self._reader_task, timeout=1.5)
            except asyncio.TimeoutError:
                self._reader_task.cancel()
        if self.status.log_lines:
            return "\n".join(self.status.log_lines)
        if self._process and self._process.stderr:
            try:
                return self._process.stderr.read().decode("utf-8", errors="replace")
            except Exception:
                pass
        return ""

    def _spawn(self, job: StreamJob) -> str | None:
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        self.status.log_lines.clear()
        try:
            creation = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creation = subprocess.CREATE_NO_WINDOW
            self._process = subprocess.Popen(
                build_ffmpeg_command(job),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=creation,
            )
        except FileNotFoundError:
            return "ffmpeg introuvable — installe ffmpeg ou verifie le PATH."
        except Exception as exc:
            return str(exc)
        self._reader_task = asyncio.create_task(self._read_stderr())
        return None

    async def _watchdog_loop(self) -> None:
        """Relance seulement si mort — avec cooldown pour eviter pause-pause-pause."""
        while self._keep_alive:
            await asyncio.sleep(8)
            if not self._keep_alive or self.is_running or not self.status.job:
                continue
            now = time.time()
            # Max 1 restart / 20s, max 15 restarts total
            if now - self._last_restart < 20:
                continue
            if self._restart_count >= 15:
                log.error("ffmpeg: trop de relances — arret watchdog")
                self.status.running = False
                self._keep_alive = False
                break
            log.warning("ffmpeg mort — relance fluide (#%s)", self._restart_count + 1)
            self._restart_count += 1
            self._last_restart = now
            err = self._spawn(self.status.job)
            if err:
                self.status.running = False
                self.status.last_error = err
                self._keep_alive = False

    async def start(self, job: StreamJob) -> StreamStatus:
        if not job.video_path.is_file():
            self.status.last_error = f"Video introuvable: {job.video_path}"
            return self.status
        if not job.outputs:
            self.status.last_error = "Aucune sortie RTMP"
            return self.status

        self._keep_alive = True
        self._restart_count = 0
        self.status.job = job
        err = self._spawn(job)
        if err:
            self.status.last_error = err
            self.status.running = False
            return self.status

        self.status.running = True
        self.status.pid = self._process.pid if self._process else None
        self.status.started_at = time.time()
        if self._watchdog and not self._watchdog.done():
            self._watchdog.cancel()
        self._watchdog = asyncio.create_task(self._watchdog_loop())

        # Laisse le temps a RTMP de se connecter sans tuer trop vite
        await asyncio.sleep(6)
        if not self.is_running:
            self.status.running = False
            self._keep_alive = False
            raw = await self._collect_stderr()
            self.status.last_error = _short_ffmpeg_error(raw)
            log.error("ffmpeg echec: %s", self.status.last_error)
            self._process = None
        return self.status

    async def stop(self) -> None:
        self._keep_alive = False
        if self._watchdog and not self._watchdog.done():
            self._watchdog.cancel()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._process and self.is_running:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        self.status.running = False


class MultiFfmpegPool:
    """Plusieurs ffmpeg en parallele — demarrage echelonne, video loop fluide."""

    def __init__(self) -> None:
        self._workers: list[FfmpegStreamer] = []
        self.last_error: str | None = None

    @property
    def is_running(self) -> bool:
        return any(w.is_running for w in self._workers)

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    async def start_chunked(
        self,
        video_path: Path,
        outputs: list[str],
        *,
        chunk_size: int = 8,
        max_workers: int = 350,
        loop: bool = True,
        overlay_path: Path | None = None,
        overlay_x: int = 0,
        overlay_y: int = 0,
    ) -> StreamStatus:
        from config import MAX_FFMPEG_WORKERS, RTMP_CHUNK_SIZE
        from video_loop import ensure_looped_video

        await self.stop()
        self.last_error = None

        # FIX pause---pause---pause: video longue pre-concatenee
        try:
            video_path = ensure_looped_video(Path(video_path))
        except Exception as exc:
            log.warning("ensure_looped_video: %s", exc)

        cs = max(1, min(chunk_size or RTMP_CHUNK_SIZE, 10))  # max 10 RTMP / process
        mw = max(1, min(max_workers, MAX_FFMPEG_WORKERS))
        mass = len(outputs) > 5

        chunks: list[list[str]] = [
            outputs[i : i + cs] for i in range(0, len(outputs), cs)
        ]
        if not chunks:
            st = StreamStatus()
            st.last_error = "Aucune sortie RTMP"
            return st

        if len(chunks) > mw:
            cs = max(1, (len(outputs) + mw - 1) // mw)
            chunks = [outputs[i : i + cs] for i in range(0, len(outputs), cs)]

        self._workers = []
        started = 0
        errors: list[str] = []

        # Demarrage echelonne (evite freeze CPU = freezes video)
        stagger = 0.35 if mass else 0.15
        for idx, chunk in enumerate(chunks):
            if idx > 0:
                await asyncio.sleep(stagger)
            worker = FfmpegStreamer()
            job = StreamJob(
                video_path=video_path,
                outputs=chunk,
                loop=loop,
                # Overlay seulement sur le 1er worker (CPU)
                overlay_path=overlay_path if started == 0 else None,
                overlay_x=overlay_x,
                overlay_y=overlay_y,
                mass_mode=mass,
            )
            status = await worker.start(job)
            if status.running:
                self._workers.append(worker)
                started += 1
                continue

            await worker.stop()
            await asyncio.sleep(1.5)
            worker2 = FfmpegStreamer()
            status2 = await worker2.start(job)
            if status2.running:
                self._workers.append(worker2)
                started += 1
            else:
                errors.append(status2.last_error or status.last_error or "ffmpeg fail")
                await worker2.stop()

        out = StreamStatus()
        out.running = started > 0
        out.started_at = time.time() if started else None
        out.pid = self._workers[0].status.pid if self._workers else None
        if not out.running:
            out.last_error = errors[0] if errors else "Aucun ffmpeg demarre"
            self.last_error = out.last_error
        elif errors:
            out.last_error = f"{started}/{len(chunks)} worker(s) OK, {len(errors)} echec(s)"
            log.warning(out.last_error)
        return out

    async def stop(self) -> None:
        for w in self._workers:
            try:
                await w.stop()
            except Exception:
                pass
        self._workers.clear()
