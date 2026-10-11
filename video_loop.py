"""Pré-concatène les vidéos courtes pour éviter pause---pause---pause à chaque boucle."""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
LOOP_CACHE_DIR = ROOT / "data" / "loop_cache"
# Au moins ~10 min de video avant rebouclage (evite freezes stream_loop)
MIN_LOOP_DURATION_SEC = 600.0
MAX_REPEAT = 40


def video_duration_seconds(path: Path) -> float:
    if not path.is_file():
        return 0.0
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return max(0.0, float(proc.stdout.strip()))
    except (ValueError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.debug("ffprobe duration %s: %s", path, exc)
    return 0.0


def _cache_key(source: Path, repeats: int) -> str:
    stat = source.stat()
    raw = f"{source.resolve()}|{stat.st_size}|{int(stat.st_mtime)}|{repeats}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_path(source: Path, repeats: int) -> Path:
    safe_name = source.stem.replace(" ", "_")[:40]
    return LOOP_CACHE_DIR / f"{safe_name}_{repeats}x_{_cache_key(source, repeats)}.mp4"


def _ffmpeg_concat_copy(source: Path, repeats: int, out: Path) -> bool:
    LOOP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    list_file = out.with_suffix(".txt")
    escaped = str(source.resolve()).replace("\\", "/").replace("'", "'\\''")
    lines = "\n".join(f"file '{escaped}'" for _ in range(repeats))
    list_file.write_text(lines, encoding="utf-8")
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if proc.returncode != 0:
            log.warning("concat copy echoue: %s", (proc.stderr or "")[-300:])
            return False
        return out.is_file() and out.stat().st_size > 10_000
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("concat copy: %s", exc)
        return False
    finally:
        list_file.unlink(missing_ok=True)


def ensure_looped_video(
    source: Path,
    *,
    min_duration: float = MIN_LOOP_DURATION_SEC,
    max_repeat: int = MAX_REPEAT,
) -> Path:
    """
    Video courte → concatene en cache (~10 min) pour éviter les freezes
    à chaque -stream_loop de ffmpeg.
    """
    if not source.is_file():
        return source

    duration = video_duration_seconds(source)
    if duration <= 0 or duration >= min_duration:
        return source

    repeats = min(max_repeat, max(2, int(min_duration / duration) + 1))
    cached = _cache_path(source, repeats)
    if cached.is_file() and cached.stat().st_size > 10_000:
        return cached

    log.info(
        "Boucle fluide: %s (%.0fs) x%s → %s",
        source.name,
        duration,
        repeats,
        cached.name,
    )
    if _ffmpeg_concat_copy(source, repeats, cached):
        return cached

    log.warning("Cache boucle indisponible — source %s", source.name)
    return source
