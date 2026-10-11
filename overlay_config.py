"""Configurations overlay live — texte + images."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import DATA_DIR, OVERLAYS_DIR

CANVAS_W = 1080
CANVAS_H = 1920
SLUG_RE = re.compile(r"[^a-z0-9_-]+")


@dataclass
class TextOverlay:
    content: str = ""
    x: int = 40
    y: int = 80
    font_size: int = 52
    color: str = "#FFFFFF"
    bg_color: str = "#00000080"
    bold: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TextOverlay:
        return cls(
            content=str(data.get("content", "")),
            x=int(data.get("x", 40)),
            y=int(data.get("y", 80)),
            font_size=int(data.get("font_size", 52)),
            color=str(data.get("color", "#FFFFFF")),
            bg_color=str(data.get("bg_color", "#00000080")),
            bold=bool(data.get("bold", True)),
        )


@dataclass
class ImageOverlay:
    filename: str = ""
    x: int = 40
    y: int = 1600
    width: int = 200
    height: int = 200
    opacity: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImageOverlay:
        return cls(
            filename=str(data.get("filename", "")),
            x=int(data.get("x", 40)),
            y=int(data.get("y", 1600)),
            width=int(data.get("width", 200)),
            height=int(data.get("height", 200)),
            opacity=float(data.get("opacity", 1.0)),
        )

    def path(self, discord_id: str) -> Path:
        return user_assets_dir(discord_id) / self.filename


@dataclass
class OverlayConfig:
    name: str = "default"
    texts: list[TextOverlay] = field(default_factory=list)
    images: list[ImageOverlay] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "texts": [t.to_dict() for t in self.texts],
            "images": [i.to_dict() for i in self.images],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OverlayConfig:
        return cls(
            name=str(data.get("name", "default")),
            texts=[TextOverlay.from_dict(t) for t in data.get("texts", [])],
            images=[ImageOverlay.from_dict(i) for i in data.get("images", [])],
        )

    def summary(self) -> str:
        parts = [f"**{self.name}**"]
        if self.texts:
            parts.append(f"Textes: {len(self.texts)}")
            for i, t in enumerate(self.texts[:3], 1):
                preview = (t.content[:30] + "…") if len(t.content) > 30 else t.content
                parts.append(f"  {i}. `{preview}` @ ({t.x},{t.y})")
        else:
            parts.append("Textes: aucun")
        if self.images:
            parts.append(f"Images: {len(self.images)}")
            for i, img in enumerate(self.images[:3], 1):
                parts.append(f"  {i}. `{img.filename}` @ ({img.x},{img.y}) {img.width}x{img.height}")
        else:
            parts.append("Images: aucune")
        return "\n".join(parts)


def slugify(name: str) -> str:
    s = name.strip().lower().replace(" ", "-")
    s = SLUG_RE.sub("-", s).strip("-")
    return s or "config"


def user_configs_dir(discord_id: str) -> Path:
    path = DATA_DIR / "overlay_configs" / str(discord_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_assets_dir(discord_id: str) -> Path:
    path = OVERLAYS_DIR / str(discord_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path(discord_id: str, name: str) -> Path:
    return user_configs_dir(discord_id) / f"{slugify(name)}.json"


def save_config(discord_id: str, config: OverlayConfig) -> Path:
    config.name = config.name.strip() or "config"
    path = config_path(discord_id, config.name)
    path.write_text(json.dumps(config.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_config(discord_id: str, name: str) -> OverlayConfig | None:
    path = config_path(discord_id, name)
    if not path.is_file():
        for p in user_configs_dir(discord_id).glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if data.get("name", "").lower() == name.strip().lower():
                return OverlayConfig.from_dict(data)
        return None
    return OverlayConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_configs(discord_id: str) -> list[OverlayConfig]:
    out: list[OverlayConfig] = []
    for p in sorted(user_configs_dir(discord_id).glob("*.json")):
        try:
            out.append(OverlayConfig.from_dict(json.loads(p.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def delete_config(discord_id: str, name: str) -> bool:
    path = config_path(discord_id, name)
    if path.is_file():
        path.unlink()
        return True
    cfg = load_config(discord_id, name)
    if not cfg:
        return False
    path = config_path(discord_id, cfg.name)
    if path.is_file():
        path.unlink()
        return True
    return False


def draft_from_session(sess: dict[str, Any]) -> OverlayConfig:
    raw = sess.get("overlay_draft") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if not raw:
        return OverlayConfig(name="brouillon")
    return OverlayConfig.from_dict(raw)


def draft_to_session_dict(config: OverlayConfig) -> dict[str, Any]:
    return {"overlay_draft": config.to_dict()}