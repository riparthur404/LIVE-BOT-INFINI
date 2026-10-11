"""Generation d'images QR code a partir d'un lien."""

from __future__ import annotations

import re
from pathlib import Path

import qrcode
from PIL import Image
from qrcode.constants import ERROR_CORRECT_M

from overlay_config import user_assets_dir

SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]+")


def normalize_qr_url(url: str) -> tuple[str | None, str | None]:
    """Normalise un lien pour le QR. Retourne (url, erreur)."""
    raw = (url or "").strip()
    if not raw:
        return None, "Lien vide."

    link = raw
    if not link.startswith(("http://", "https://")):
        if link.startswith(("discord.gg/", "www.")) or ("." in link and " " not in link):
            link = "https://" + link
        else:
            return None, "Format invalide. Exemple: `https://discord.gg/xxx`"

    if " " in link or len(link) < 8:
        return None, "Lien trop court ou invalide."

    return link, None


def _safe_slug(name: str) -> str:
    s = SAFE_NAME.sub("_", name.strip())[:30].strip("_")
    return s or "qr"


def generate_qr_image(
    user_id: int | str,
    url: str,
    *,
    size: int = 280,
    name: str = "qr",
) -> Path:
    link, err = normalize_qr_url(url)
    if err or not link:
        raise ValueError(err or "URL invalide.")

    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    target = max(120, min(600, int(size)))
    img = img.resize((target, target), Image.Resampling.LANCZOS)

    out_dir = user_assets_dir(str(user_id))
    out_path = out_dir / f"{_safe_slug(name)}.png"
    img.save(out_path, "PNG")
    return out_path