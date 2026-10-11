#!/usr/bin/env python3
"""Point d'entree PebbleHost — capture les erreurs de demarrage."""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
BOOT_LOG = LOG_DIR / "boot.log"

_REQUIRED_AT_ROOT = (
    "bot.py",
    "public_commands.py",
    "live_engine.py",
    "tiktok_live.py",
    "config.py",
    "database.py",
    "requirements.txt",
)


def _log(msg: str) -> None:
    print(msg, flush=True)
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with BOOT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except OSError:
        pass


def main() -> None:
    _log("=== Fazox start.py ===")
    _log(f"Python {sys.version}")
    _log(f"CWD: {Path.cwd()}")
    _log(f"ROOT: {ROOT}")
    _log(f"__main__.__file__ = {getattr(sys.modules.get('__main__'), '__file__', '?')}")
    if sys.version_info < (3, 10):
        _log("ATTENTION: Python 3.9 detecte — passe a 3.11 dans Bot Settings si possible.")

    missing = [name for name in _REQUIRED_AT_ROOT if not (ROOT / name).is_file()]
    if missing:
        _log("=" * 60)
        _log("ERREUR — fichiers manquants dans /home/container/ :")
        for name in missing:
            _log(f"  - {name}")
        _log("")
        _log("Tu as probablement upload seulement quelques .py.")
        _log("Upload bot.py + start.py + tout le dossier a la RACINE.")
        _log("Ne supprime PAS .env ni data/fazox.db")
        _log("=" * 60)
        raise SystemExit(1)

    try:
        from bot import main as run_bot

        run_bot()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        _log(f"Arret SystemExit ({code}): {exc}")
        raise
    except Exception:
        _log("ERREUR FATALE au demarrage:")
        tb = traceback.format_exc()
        _log(tb)
        raise


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(1)