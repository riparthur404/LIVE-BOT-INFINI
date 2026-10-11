"""Bibliothèque de recs — scan auto des vidéos présentes sur le disque."""

from __future__ import annotations

import json
import re
from pathlib import Path

from config import CATALOG_PATH, EXTRA_VIDEO_DIRS, RECS_DIR, VIDEOS_DIR

EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


def _video_dirs() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for folder in (RECS_DIR, VIDEOS_DIR, *EXTRA_VIDEO_DIRS):
        resolved = folder.resolve()
        if resolved.is_dir() and resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def _load_catalog() -> dict[str, str]:
    if not CATALOG_PATH.exists():
        return {}
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_catalog(catalog: dict[str, str]) -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(
        json.dumps(dict(sorted(catalog.items())), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def normalize_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower().replace("_", " "))


def _find_file(filename: str) -> Path | None:
    for folder in _video_dirs():
        candidate = folder / filename
        if candidate.is_file():
            return candidate.resolve()
    return None


def discover_videos() -> dict[str, Path]:
    """Toutes les vidéos trouvées sur le disque, clé = nom normalisé du fichier."""
    found: dict[str, Path] = {}
    for folder in _video_dirs():
        for f in sorted(folder.iterdir()):
            if not f.is_file() or f.suffix.lower() not in EXTENSIONS:
                continue
            path = f.resolve()
            keys = {normalize_key(f.stem), normalize_key(f.name)}
            for key in keys:
                if key and key not in found:
                    found[key] = path
    return found


def _prune_catalog() -> dict[str, str]:
    """Garde uniquement les entrées dont le fichier existe."""
    catalog = _load_catalog()
    pruned: dict[str, str] = {}
    for key, filename in catalog.items():
        if _find_file(filename):
            pruned[normalize_key(key)] = filename
    return pruned


def sync_catalog() -> dict[str, str]:
    """Catalogue = entrées valides + auto-détection des mp4 présents."""
    catalog = _prune_catalog()
    for key, path in discover_videos().items():
        catalog.setdefault(key, path.name)
    _save_catalog(catalog)
    return catalog


def resolve_rec(game: str) -> Path | None:
    catalog = sync_catalog()
    key = normalize_key(game)

    filename = catalog.get(key)
    if filename:
        found = _find_file(filename)
        if found:
            return found

    for k, v in catalog.items():
        if normalize_key(k) == key:
            found = _find_file(v)
            if found:
                return found

    discovered = discover_videos()
    if key in discovered:
        return discovered[key]

    for dk, path in discovered.items():
        if key in dk or dk in key:
            return path

    return None


def list_games() -> list[tuple[str, Path]]:
    """Liste uniquement les jeux/recs avec fichier vidéo présent."""
    catalog = sync_catalog()
    seen_paths: set[str] = set()
    out: list[tuple[str, Path]] = []

    for key, filename in sorted(catalog.items(), key=lambda x: x[0]):
        path = _find_file(filename)
        if not path:
            continue
        path_s = str(path)
        if path_s in seen_paths:
            continue
        seen_paths.add(path_s)
        out.append((key, path))

    for key, path in sorted(discover_videos().items(), key=lambda x: x[0]):
        path_s = str(path)
        if path_s in seen_paths:
            continue
        seen_paths.add(path_s)
        out.append((key, path))

    return out


def first_available_video() -> Path | None:
    games = list_games()
    return games[0][1] if games else None


def resolve_video_path(saved: str | None) -> Path | None:
    """Utilise le chemin enregistré ou la première vidéo disponible."""
    if saved:
        p = Path(saved)
        if p.is_file():
            return p.resolve()
    return first_available_video()


def game_choices_for_discord(limit: int = 25) -> list[tuple[str, str]]:
    """(label, value) pour select Discord — vidéos disponibles seulement."""
    items: list[tuple[str, str]] = []
    for key, path in list_games()[:limit]:
        label = f"{key.title()} — {path.name}"
        items.append((label[:100], key))
    return items