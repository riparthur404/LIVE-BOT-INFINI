"""Rendu preview + PNG overlay transparent pour ffmpeg."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from overlay_config import (
    CANVAS_H,
    CANVAS_W,
    ImageOverlay,
    OverlayConfig,
    TextOverlay,
    user_assets_dir,
)

log = logging.getLogger(__name__)

_FRAME_CACHE: dict[str, Image.Image] = {}


def _frame_cache_key(discord_id: str, video_path: Path | None) -> str:
    if video_path and video_path.is_file():
        st = video_path.stat()
        return f"{discord_id}:{video_path.resolve()}:{st.st_mtime_ns}:{st.st_size}"
    return f"{discord_id}:none"


def get_cached_frame(discord_id: str, video_path: Path | None) -> Image.Image | None:
    return _FRAME_CACHE.get(_frame_cache_key(discord_id, video_path))


def invalidate_frame_cache(discord_id: str, video_path: Path | None) -> None:
    _FRAME_CACHE.pop(_frame_cache_key(discord_id, video_path), None)


def cache_video_frame(discord_id: str, video_path: Path | None) -> Image.Image:
    """Extrait une frame video (ffmpeg) et la met en cache — appeler dans un thread."""
    key = _frame_cache_key(discord_id, video_path)
    cached = _FRAME_CACHE.get(key)
    if cached is not None:
        return cached

    if video_path and video_path.is_file():
        frame_path = user_assets_dir(discord_id) / "_editor_frame.jpg"
        extracted = extract_best_video_frame(video_path, frame_path)
        if extracted:
            base = Image.open(extracted).convert("RGBA")
            frame_path.unlink(missing_ok=True)
        else:
            base = _blank_preview_bg("Video illisible — verifie /jeu")
    else:
        base = _blank_preview_bg("Choisis une video avec /jeu")

    _FRAME_CACHE[key] = base
    return base


@dataclass
class OverlayPlacement:
    """PNG compact (QR/texte seulement) + position sur le canvas 1080x1920."""

    path: Path
    x: int = 0
    y: int = 0


PROBE_TIMES = (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0)
PAD_COLOR = "0x1a1a2e"

_FONT_REGULAR_CANDIDATES = (
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
)
_FONT_BOLD_CANDIDATES = (
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
)

FONT_REGULAR = next((p for p in _FONT_REGULAR_CANDIDATES if p.is_file()), Path("arial.ttf"))
FONT_BOLD = next((p for p in _FONT_BOLD_CANDIDATES if p.is_file()), FONT_REGULAR)


def _video_scale_pad_vf() -> str:
    return (
        f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease,"
        f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:color={PAD_COLOR}"
    )


def _image_brightness(img: Image.Image) -> float:
    rgb = img.convert("RGB").resize((128, 128))
    pixels = list(rgb.getdata())
    if not pixels:
        return 0.0
    return sum((p[0] + p[1] + p[2]) / 3.0 for p in pixels) / len(pixels)


def _enhance_for_preview(img: Image.Image) -> Image.Image:
    if _image_brightness(img) < 55:
        img = ImageEnhance.Brightness(img).enhance(1.7)
        img = ImageEnhance.Contrast(img).enhance(1.25)
    return img


def _hex_to_rgba(color: str, default: tuple[int, int, int, int] = (255, 255, 255, 255)) -> tuple[int, int, int, int]:
    c = (color or "").strip()
    if not c.startswith("#"):
        return default
    c = c[1:]
    if len(c) == 6:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        return r, g, b, 255
    if len(c) == 8:
        r, g, b, a = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), int(c[6:8], 16)
        return r, g, b, a
    return default


def _load_font(size: int, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = FONT_BOLD if bold and FONT_BOLD.is_file() else FONT_REGULAR
    if path.is_file():
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    return ImageFont.load_default()


def _text_bbox(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int, int, int]:
    if hasattr(draw, "textbbox"):
        return draw.textbbox((0, 0), text, font=font)
    w, h = draw.textsize(text, font=font)
    return 0, 0, w, h


def _draw_text(draw: ImageDraw.ImageDraw, item: TextOverlay) -> None:
    if not item.content.strip():
        return
    font = _load_font(item.font_size, item.bold)
    bbox = _text_bbox(draw, item.content, font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 12
    bg = _hex_to_rgba(item.bg_color, (0, 0, 0, 128))
    if bg[3] > 0:
        draw.rectangle(
            [item.x - pad, item.y - pad, item.x + tw + pad, item.y + th + pad],
            fill=bg,
        )
    fg = _hex_to_rgba(item.color)
    draw.text((item.x, item.y), item.content, font=font, fill=fg)


def _overlay_bounds(
    config: OverlayConfig,
    discord_id: str,
) -> tuple[int, int, int, int] | None:
    """min_x, min_y, max_x, max_y dans le canvas live."""
    boxes: list[tuple[int, int, int, int]] = []
    tmp = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(tmp)

    for item in config.texts:
        if not item.content.strip():
            continue
        font = _load_font(item.font_size, item.bold)
        bbox = _text_bbox(draw, item.content, font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = 12
        boxes.append(
            (item.x - pad, item.y - pad, item.x + tw + pad, item.y + th + pad)
        )

    for item in config.images:
        if not item.filename:
            continue
        src = item.path(discord_id)
        if not src.is_file():
            continue
        boxes.append(
            (item.x, item.y, item.x + max(1, item.width), item.y + max(1, item.height))
        )

    if not boxes:
        return None

    min_x = max(0, min(b[0] for b in boxes))
    min_y = max(0, min(b[1] for b in boxes))
    max_x = min(CANVAS_W, max(b[2] for b in boxes))
    max_y = min(CANVAS_H, max(b[3] for b in boxes))
    if max_x <= min_x or max_y <= min_y:
        return None
    return min_x, min_y, max_x, max_y


def _paste_image(canvas: Image.Image, item: ImageOverlay, discord_id: str) -> None:
    if not item.filename:
        return
    src = item.path(discord_id)
    if not src.is_file():
        log.warning("Image overlay introuvable: %s", src)
        return
    try:
        img = Image.open(src).convert("RGBA")
    except OSError as exc:
        log.warning("Image overlay illisible %s: %s", src, exc)
        return
    img = img.resize((max(1, item.width), max(1, item.height)), Image.Resampling.LANCZOS)
    if item.opacity < 1.0:
        alpha = img.split()[3]
        alpha = alpha.point(lambda p: int(p * max(0.0, min(1.0, item.opacity))))
        img.putalpha(alpha)
    canvas.paste(img, (item.x, item.y), img)


def render_overlay_png(config: OverlayConfig, discord_id: str, out_path: Path) -> OverlayPlacement:
    """PNG compact transparent — evite l'ecran noir ffmpeg (overlay plein ecran)."""
    bounds = _overlay_bounds(config, discord_id)
    if not bounds:
        raise ValueError("Overlay vide")

    min_x, min_y, max_x, max_y = bounds
    w, h = max_x - min_x, max_y - min_y
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    for item in config.texts:
        if not item.content.strip():
            continue
        shifted = TextOverlay(
            content=item.content,
            x=item.x - min_x,
            y=item.y - min_y,
            font_size=item.font_size,
            color=item.color,
            bg_color=item.bg_color,
            bold=item.bold,
        )
        _draw_text(draw, shifted)

    for item in config.images:
        if not item.filename:
            continue
        shifted = ImageOverlay(
            filename=item.filename,
            x=item.x - min_x,
            y=item.y - min_y,
            width=item.width,
            height=item.height,
            opacity=item.opacity,
        )
        _paste_image(canvas, shifted, discord_id)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "PNG", compress_level=3)
    return OverlayPlacement(path=out_path, x=min_x, y=min_y)


def _extract_frame_at(video_path: Path, out_path: Path, at_sec: float) -> bool:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-ss",
        str(at_sec),
        "-frames:v",
        "1",
        "-vf",
        _video_scale_pad_vf(),
        "-q:v",
        "2",
        "-pix_fmt",
        "yuvj420p",
        "-y",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=45)
        return out_path.is_file() and out_path.stat().st_size > 1000
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.debug("Frame @%ss: %s", at_sec, exc)
        return False


def extract_best_video_frame(video_path: Path, out_path: Path) -> Path | None:
    """Evite les frames noires au debut de la video."""
    if not video_path.is_file():
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    best_img: Image.Image | None = None
    best_score = -1.0

    for at_sec in PROBE_TIMES:
        probe = out_path.with_name(f"{out_path.stem}_probe_{at_sec}.jpg")
        if not _extract_frame_at(video_path, probe, at_sec):
            probe.unlink(missing_ok=True)
            continue
        try:
            img = Image.open(probe).convert("RGB")
            score = _image_brightness(img)
            if score > best_score:
                best_score = score
                best_img = img.copy()
        except OSError:
            pass
        finally:
            probe.unlink(missing_ok=True)

    if best_img is None:
        log.warning("Aucune frame lisible pour %s", video_path)
        return None

    best_img = _enhance_for_preview(best_img)
    best_img.save(out_path, "JPEG", quality=92)
    return out_path


def render_preview_fast(
    config: OverlayConfig,
    discord_id: str,
    video_path: Path | None,
    out_path: Path,
    *,
    base_frame: Image.Image | None = None,
) -> Path:
    """Apercu rapide (PIL seulement) — pas de ffmpeg."""
    if base_frame is not None:
        base = base_frame.copy()
    else:
        cached = get_cached_frame(discord_id, video_path)
        if cached is not None:
            base = cached.copy()
        elif video_path and video_path.is_file():
            base = _blank_preview_bg("Chargement video...")
        else:
            base = _blank_preview_bg("Choisis une video avec /jeu")

    overlay = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for item in config.texts:
        _draw_text(draw, item)
    for item in config.images:
        _paste_image(overlay, item, discord_id)

    preview = Image.alpha_composite(base, overlay).convert("RGB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    preview.save(out_path, "JPEG", quality=90)
    return out_path


def render_preview(
    config: OverlayConfig,
    discord_id: str,
    video_path: Path | None,
    out_path: Path,
) -> Path:
    """Apercu visuel: frame video + overlays (ffmpeg si pas en cache)."""
    base = cache_video_frame(discord_id, video_path)
    return render_preview_fast(config, discord_id, video_path, out_path, base_frame=base)


def _blank_preview_bg(label: str = "APERCU LIVE") -> Image.Image:
    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (26, 26, 46, 255))
    draw = ImageDraw.Draw(img)
    draw.text((CANVAS_W // 2 - 160, CANVAS_H // 2 - 20), label[:40], fill=(140, 140, 170, 255))
    return img


def overlay_png_path(discord_id: str, config_name: str) -> Path:
    safe = config_name.replace(" ", "_")
    return user_assets_dir(discord_id) / f"{safe}_overlay.png"


def preview_path(discord_id: str, config_name: str) -> Path:
    safe = config_name.replace(" ", "_")
    return user_assets_dir(discord_id) / f"{safe}_preview.jpg"