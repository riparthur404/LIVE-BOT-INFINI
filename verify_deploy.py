"""Verifie que tous les fichiers sont presents avant de lancer bot.py."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
REQUIRED = [
    "bot.py",
    "start.py",
    "pebble-python-config.json",
    "public_commands.py",
    "live_engine.py",
    "tiktok_live.py",
    "config.py",
    "database.py",
    "license.py",
    "overlay_editor.py",
    "streamer.py",
    "requirements.txt",
]

missing = [p for p in REQUIRED if not (ROOT / p).is_file()]
if missing:
    print("Fichiers manquants:")
    for p in missing:
        print(f"  - {p}")
    sys.exit(1)
print("OK — deploy complet ({0} fichiers)".format(len(REQUIRED)))
sys.exit(0)