"""SQLite — comptes, cookies dédupliqués, sessions live."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import DB_PATH, DATA_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cookie_hash(cookies: dict[str, str] | str) -> str:
    if isinstance(cookies, dict):
        payload = json.dumps(sorted(cookies.items()), ensure_ascii=False)
    else:
        payload = cookies.strip()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class LicenseKey:
    key: str
    dualhook: bool
    expires_at: str | None
    created_by: str
    created_at: str
    revoked: bool
    used_by_discord_id: str | None = None
    used_by_name: str | None = None


@dataclass
class UserLicense:
    discord_id: str
    license_key: str
    dualhook: bool
    expires_at: str | None
    linked_at: str
    valid: bool
    reason: str = ""


DEFAULT_CONFIG = "main"
MAX_CONFIGS_PER_USER = 10
CONFIG_NAME_RE = re.compile(r"^[a-z0-9_-]{2,24}$")


@dataclass
class StoredAccount:
    id: int
    discord_id: str
    discord_name: str
    platform: str
    account_pseudo: str
    cookies_json: str
    cookie_hash: str
    stream_key: str
    rtmp_url: str
    full_push_url: str
    room_id: str
    bio: str
    bio_link: str
    original_bio_link: str
    live_title: str
    updated_at: str
    config_name: str = DEFAULT_CONFIG


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id TEXT NOT NULL,
                    discord_name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    account_pseudo TEXT NOT NULL,
                    cookies_json TEXT DEFAULT '',
                    cookie_hash TEXT DEFAULT '',
                    stream_key TEXT DEFAULT '',
                    rtmp_url TEXT DEFAULT '',
                    full_push_url TEXT DEFAULT '',
                    room_id TEXT DEFAULT '',
                    bio TEXT DEFAULT '',
                    bio_link TEXT DEFAULT '',
                    live_title TEXT DEFAULT 'Live',
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, account_pseudo)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_cookie_hash
                    ON accounts(cookie_hash) WHERE cookie_hash != '';
                CREATE TABLE IF NOT EXISTS sessions (
                    discord_id TEXT PRIMARY KEY,
                    discord_name TEXT,
                    game_key TEXT DEFAULT '',
                    video_path TEXT DEFAULT '',
                    live_title TEXT DEFAULT 'Live',
                    bio TEXT DEFAULT '',
                    bio_link TEXT DEFAULT '',
                    platforms_json TEXT DEFAULT '[]',
                    overlay_config_name TEXT DEFAULT '',
                    overlay_draft_json TEXT DEFAULT '',
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS license_keys (
                    key TEXT PRIMARY KEY,
                    dualhook INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS user_licenses (
                    discord_id TEXT PRIMARY KEY,
                    license_key TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    FOREIGN KEY (license_key) REFERENCES license_keys(key)
                );
                CREATE TABLE IF NOT EXISTS cookie_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id TEXT NOT NULL,
                    discord_name TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'tiktok',
                    account_pseudo TEXT NOT NULL,
                    action TEXT NOT NULL,
                    bio_link TEXT DEFAULT '',
                    cookie_hash TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                """
            )
            for col, ddl in (
                ("overlay_config_name", "TEXT DEFAULT ''"),
                ("overlay_draft_json", "TEXT DEFAULT ''"),
                ("autolive_minutes", "INTEGER DEFAULT 14"),
                ("autolive_platforms_json", "TEXT DEFAULT '[]'"),
                ("bio_schedule_enabled", "INTEGER DEFAULT 0"),
            ):
                try:
                    conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    pass
            for col, ddl in (("original_bio_link", "TEXT DEFAULT ''"),):
                try:
                    conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute(
                    f"ALTER TABLE accounts ADD COLUMN config_name TEXT DEFAULT '{DEFAULT_CONFIG}'"
                )
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    f"ALTER TABLE sessions ADD COLUMN active_config TEXT DEFAULT '{DEFAULT_CONFIG}'"
                )
            except sqlite3.OperationalError:
                pass
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS config_profiles (
                    discord_id TEXT NOT NULL,
                    config_name TEXT NOT NULL,
                    discord_name TEXT DEFAULT '',
                    game_key TEXT DEFAULT '',
                    video_path TEXT DEFAULT '',
                    live_title TEXT DEFAULT 'Live',
                    bio TEXT DEFAULT '',
                    bio_link TEXT DEFAULT '',
                    platforms_json TEXT DEFAULT '[]',
                    overlay_config_name TEXT DEFAULT '',
                    overlay_draft_json TEXT DEFAULT '',
                    autolive_minutes INTEGER DEFAULT 14,
                    autolive_platforms_json TEXT DEFAULT '[]',
                    bio_schedule_enabled INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (discord_id, config_name)
                );
                """
            )
            conn.execute(
                "UPDATE accounts SET config_name=? WHERE config_name IS NULL OR config_name=''",
                (DEFAULT_CONFIG,),
            )
            conn.commit()

    @staticmethod
    def normalize_config_name(name: str) -> str:
        raw = (name or "").strip().lower().replace(" ", "_")
        raw = re.sub(r"[^a-z0-9_-]", "", raw)
        if not raw or not CONFIG_NAME_RE.match(raw):
            raise ValueError(
                "Nom invalide (2-24 caracteres: lettres, chiffres, _ ou -). Ex: config2"
            )
        if raw == DEFAULT_CONFIG:
            return raw
        return raw

    def get_active_config_name(self, discord_id: str) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT active_config FROM sessions WHERE discord_id=?",
                (str(discord_id),),
            ).fetchone()
        if row and row["active_config"]:
            return str(row["active_config"])
        return DEFAULT_CONFIG

    def set_active_config(self, discord_id: str, discord_name: str, config_name: str) -> None:
        config_name = self.normalize_config_name(config_name)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sessions (discord_id, discord_name, active_config, updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    discord_name=excluded.discord_name,
                    active_config=excluded.active_config,
                    updated_at=excluded.updated_at
                """,
                (str(discord_id), discord_name, config_name, _now()),
            )
            conn.commit()

    def _empty_profile(self, discord_id: str, config_name: str, discord_name: str = "") -> dict[str, Any]:
        return {
            "discord_id": str(discord_id),
            "config_name": config_name,
            "discord_name": discord_name,
            "game_key": "",
            "video_path": "",
            "live_title": "Live",
            "bio": "",
            "bio_link": "",
            "platforms": [],
            "overlay_config_name": "",
            "overlay_draft": {},
            "autolive_minutes": 14,
            "autolive_platforms": [],
            "bio_schedule_enabled": False,
        }

    def _profile_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        draft: dict[str, Any] = {}
        raw_draft = row["overlay_draft_json"] or ""
        if raw_draft:
            try:
                draft = json.loads(raw_draft)
            except json.JSONDecodeError:
                draft = {}
        return {
            "discord_id": row["discord_id"],
            "config_name": row["config_name"],
            "discord_name": row["discord_name"] or "",
            "game_key": row["game_key"] or "",
            "video_path": row["video_path"] or "",
            "live_title": row["live_title"] or "Live",
            "bio": row["bio"] or "",
            "bio_link": row["bio_link"] or "",
            "platforms": json.loads(row["platforms_json"] or "[]"),
            "overlay_config_name": row["overlay_config_name"] or "",
            "overlay_draft": draft,
            "autolive_minutes": int(row["autolive_minutes"] or 14),
            "autolive_platforms": json.loads(row["autolive_platforms_json"] or "[]"),
            "bio_schedule_enabled": bool(row["bio_schedule_enabled"]),
        }

    def get_config_profile(self, discord_id: str, config_name: str) -> dict[str, Any] | None:
        config_name = self.normalize_config_name(config_name)
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM config_profiles
                WHERE discord_id=? AND config_name=?
                """,
                (str(discord_id), config_name),
            ).fetchone()
        return self._profile_row_to_dict(row) if row else None

    def save_config_profile(
        self, discord_id: str, discord_name: str, config_name: str, **fields: Any
    ) -> dict[str, Any]:
        config_name = self.normalize_config_name(config_name)
        current = self.get_config_profile(discord_id, config_name) or self._empty_profile(
            discord_id, config_name, discord_name
        )
        current.update(fields)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO config_profiles (
                    discord_id, config_name, discord_name, game_key, video_path, live_title,
                    bio, bio_link, platforms_json, overlay_config_name, overlay_draft_json,
                    autolive_minutes, autolive_platforms_json, bio_schedule_enabled,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(discord_id, config_name) DO UPDATE SET
                    discord_name=excluded.discord_name,
                    game_key=excluded.game_key,
                    video_path=excluded.video_path,
                    live_title=excluded.live_title,
                    bio=excluded.bio,
                    bio_link=excluded.bio_link,
                    platforms_json=excluded.platforms_json,
                    overlay_config_name=excluded.overlay_config_name,
                    overlay_draft_json=excluded.overlay_draft_json,
                    autolive_minutes=excluded.autolive_minutes,
                    autolive_platforms_json=excluded.autolive_platforms_json,
                    bio_schedule_enabled=excluded.bio_schedule_enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    str(discord_id),
                    config_name,
                    discord_name,
                    current.get("game_key", ""),
                    current.get("video_path", ""),
                    current.get("live_title", "Live"),
                    current.get("bio", ""),
                    current.get("bio_link", ""),
                    json.dumps(current.get("platforms", [])),
                    current.get("overlay_config_name", ""),
                    json.dumps(current.get("overlay_draft", {}), ensure_ascii=False),
                    int(current.get("autolive_minutes", 14) or 14),
                    json.dumps(current.get("autolive_platforms", [])),
                    1 if current.get("bio_schedule_enabled") else 0,
                    _now(),
                    _now(),
                ),
            )
            conn.commit()
        return current

    def list_config_profiles(self, discord_id: str) -> list[str]:
        names = {DEFAULT_CONFIG}
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT config_name FROM config_profiles WHERE discord_id=? ORDER BY config_name",
                (str(discord_id),),
            ).fetchall()
            for row in rows:
                names.add(row["config_name"])
            acc_rows = conn.execute(
                """
                SELECT DISTINCT config_name FROM accounts
                WHERE discord_id=? AND config_name IS NOT NULL AND config_name != ''
                """,
                (str(discord_id),),
            ).fetchall()
            for row in acc_rows:
                names.add(row["config_name"] or DEFAULT_CONFIG)
        return sorted(names)

    def get_main_session_field(self, discord_id: str, field: str) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT game_key, video_path, live_title FROM sessions WHERE discord_id=?",
                (str(discord_id),),
            ).fetchone()
        if not row:
            return ""
        return str(row[field] if field in row.keys() else "") or ""

    def count_accounts_in_config(self, discord_id: str, config_name: str) -> int:
        config_name = self.normalize_config_name(config_name)
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM accounts
                WHERE discord_id=? AND config_name=?
                AND (cookies_json != '' OR stream_key != '')
                """,
                (str(discord_id), config_name),
            ).fetchone()
        return int(row["n"]) if row else 0

    def create_config_profile(
        self, discord_id: str, discord_name: str, config_name: str
    ) -> tuple[bool, str]:
        config_name = self.normalize_config_name(config_name)
        if config_name == DEFAULT_CONFIG:
            return False, "La config `main` existe deja — c'est ta config par defaut."
        profiles = self.list_config_profiles(discord_id)
        extra = [p for p in profiles if p != DEFAULT_CONFIG]
        if config_name in profiles:
            self.set_active_config(discord_id, discord_name, config_name)
            return True, f"Config **{config_name}** deja existante — activee."
        if len(extra) >= MAX_CONFIGS_PER_USER - 1:
            return False, f"Maximum {MAX_CONFIGS_PER_USER} configs par utilisateur."
        self.save_config_profile(discord_id, discord_name, config_name)
        self.set_active_config(discord_id, discord_name, config_name)
        return True, (
            f"Config **{config_name}** creee et activee.\n"
            f"Ajoute des comptes avec `/addtiktok` — ils seront separes de `main`.\n"
            f"Choisis un autre replay avec `/jeu` sur cette config."
        )

    def log_cookie_action(
        self,
        *,
        discord_id: str,
        discord_name: str,
        account_pseudo: str,
        action: str,
        platform: str = "tiktok",
        bio_link: str = "",
        cookies: dict[str, str] | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO cookie_audit (
                    discord_id, discord_name, platform, account_pseudo,
                    action, bio_link, cookie_hash, updated_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    str(discord_id),
                    discord_name,
                    platform.lower(),
                    account_pseudo.lower().lstrip("@"),
                    action,
                    bio_link,
                    cookie_hash(cookies) if cookies else "",
                    _now(),
                ),
            )
            conn.commit()

    def update_account_fields(
        self,
        discord_id: str,
        platform: str,
        pseudo: str,
        *,
        bio: str | None = None,
        bio_link: str | None = None,
        original_bio_link: str | None = None,
        live_title: str | None = None,
        discord_name: str | None = None,
    ) -> StoredAccount | None:
        pseudo = pseudo.lower().lstrip("@")
        platform = platform.lower()
        sets: list[str] = ["updated_at=?"]
        vals: list[Any] = [_now()]
        if bio is not None:
            sets.append("bio=?")
            vals.append(bio)
        if bio_link is not None:
            sets.append("bio_link=?")
            vals.append(bio_link)
        if original_bio_link is not None:
            sets.append("original_bio_link=?")
            vals.append(original_bio_link)
        if live_title is not None:
            sets.append("live_title=?")
            vals.append(live_title)
        if discord_name:
            sets.append("discord_name=?")
            vals.append(discord_name)
        vals.extend([str(discord_id), platform, pseudo])
        with self._conn() as conn:
            cur = conn.execute(
                f"""
                UPDATE accounts SET {", ".join(sets)}
                WHERE discord_id=? AND platform=? AND account_pseudo=?
                """,
                vals,
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                """
                SELECT id FROM accounts
                WHERE discord_id=? AND platform=? AND account_pseudo=?
                """,
                (str(discord_id), platform, pseudo),
            ).fetchone()
        return self.get_by_id(row["id"]) if row else None  # type: ignore[arg-type]

    def update_account_bio_link(
        self,
        pseudo: str,
        bio_link: str,
        *,
        discord_id: str | None = None,
        discord_name: str | None = None,
        platform: str = "tiktok",
    ) -> StoredAccount | None:
        if not discord_id:
            return None
        return self.update_account_fields(
            str(discord_id),
            platform,
            pseudo,
            bio_link=bio_link,
            discord_name=discord_name,
        )

    def list_cookie_audit(self, discord_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if discord_id:
                rows = conn.execute(
                    """
                    SELECT * FROM cookie_audit WHERE discord_id=?
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (str(discord_id), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cookie_audit ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_account_by_cookie_hash(
        self,
        chash: str,
        discord_id: str | None = None,
    ) -> StoredAccount | None:
        if not chash:
            return None
        with self._conn() as conn:
            if discord_id:
                row = conn.execute(
                    """
                    SELECT id FROM accounts
                    WHERE cookie_hash=? AND discord_id=?
                    """,
                    (chash, str(discord_id)),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM accounts WHERE cookie_hash=?",
                    (chash,),
                ).fetchone()
        return self.get_by_id(row["id"]) if row else None  # type: ignore[arg-type]

    def is_pseudo_taken(self, platform: str, pseudo: str) -> bool:
        platform = platform.lower()
        pseudo = pseudo.lower().lstrip("@")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM accounts WHERE platform=? AND account_pseudo=? LIMIT 1",
                (platform, pseudo),
            ).fetchone()
        return row is not None

    def upsert_account(
        self,
        *,
        discord_id: str,
        discord_name: str,
        platform: str,
        account_pseudo: str,
        cookies: dict[str, str] | None = None,
        stream_key: str = "",
        rtmp_url: str = "",
        full_push_url: str = "",
        room_id: str = "",
        config_name: str | None = None,
    ) -> tuple[StoredAccount, bool]:
        """Retourne (compte, created). Pas de doublon cookie_hash."""
        platform = platform.lower()
        pseudo = account_pseudo.lower().lstrip("@")
        cfg = self.normalize_config_name(config_name or self.get_active_config_name(discord_id))
        cjson = json.dumps(cookies or {}, ensure_ascii=False)
        chash = cookie_hash(cookies) if cookies else ""

        with self._conn() as conn:
            if chash:
                row = conn.execute(
                    """
                    SELECT id, discord_id, account_pseudo, config_name
                    FROM accounts WHERE cookie_hash = ?
                    """,
                    (chash,),
                ).fetchone()
                if row and str(row["discord_id"]) != str(discord_id):
                    raise ValueError(
                        "Ce cookie est deja lie a un autre utilisateur (cle differente)."
                    )
                if row:
                    conn.execute(
                        """
                        UPDATE accounts SET
                            discord_id=?, discord_name=?, config_name=?, cookies_json=?,
                            stream_key=?, rtmp_url=?, full_push_url=?, room_id=?, updated_at=?
                        WHERE cookie_hash=?
                        """,
                        (
                            discord_id,
                            discord_name,
                            cfg,
                            cjson,
                            stream_key,
                            rtmp_url,
                            full_push_url,
                            room_id,
                            _now(),
                            chash,
                        ),
                    )
                    conn.commit()
                    acc = self.get_by_id(row["id"])  # type: ignore[arg-type]
                    self.log_cookie_action(
                        discord_id=discord_id,
                        discord_name=discord_name,
                        account_pseudo=acc.account_pseudo,
                        action="cookie_update",
                        cookies=cookies,
                    )
                    return acc, False

            existing = conn.execute(
                "SELECT id, discord_id, cookie_hash FROM accounts WHERE platform=? AND account_pseudo=?",
                (platform, pseudo),
            ).fetchone()

            if existing and str(existing["discord_id"]) != str(discord_id):
                raise ValueError(
                    f"@{pseudo} est deja enregistre par un autre utilisateur."
                )

            # Meme pseudo mais cookie different = nouveau compte (pseudo doit etre unique avant)
            if existing and chash and str(existing["cookie_hash"] or "") != chash:
                raise ValueError(
                    f"@{pseudo} existe deja avec un autre cookie — utilise un pseudo different."
                )

            if existing:
                conn.execute(
                    """
                    UPDATE accounts SET discord_id=?, discord_name=?, config_name=?,
                        cookies_json=?, cookie_hash=?, stream_key=?, rtmp_url=?,
                        full_push_url=?, room_id=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        discord_id,
                        discord_name,
                        cfg,
                        cjson,
                        chash,
                        stream_key,
                        rtmp_url,
                        full_push_url,
                        room_id,
                        _now(),
                        existing["id"],
                    ),
                )
                conn.commit()
                acc = self.get_by_id(existing["id"])  # type: ignore[arg-type]
                if cookies:
                    self.log_cookie_action(
                        discord_id=discord_id,
                        discord_name=discord_name,
                        account_pseudo=pseudo,
                        action="cookie_update",
                        cookies=cookies,
                    )
                return acc, False

            cur = conn.execute(
                """
                INSERT INTO accounts (
                    discord_id, discord_name, platform, account_pseudo, config_name,
                    cookies_json, cookie_hash, stream_key, rtmp_url, full_push_url,
                    room_id, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    discord_id,
                    discord_name,
                    platform,
                    pseudo,
                    cfg,
                    cjson,
                    chash,
                    stream_key,
                    rtmp_url,
                    full_push_url,
                    room_id,
                    _now(),
                ),
            )
            conn.commit()
            acc = self.get_by_id(cur.lastrowid)  # type: ignore[arg-type]
            if cookies:
                self.log_cookie_action(
                    discord_id=discord_id,
                    discord_name=discord_name,
                    account_pseudo=pseudo,
                    action="cookie_save",
                    cookies=cookies,
                )
            return acc, True

    def get_by_id(self, acc_id: int) -> StoredAccount:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
        return self._row(row)

    def get_account(
        self,
        platform: str,
        pseudo: str,
        discord_id: str | None = None,
    ) -> StoredAccount | None:
        pseudo = pseudo.lower().lstrip("@")
        platform = platform.lower()
        with self._conn() as conn:
            if discord_id:
                row = conn.execute(
                    """
                    SELECT * FROM accounts
                    WHERE platform=? AND account_pseudo=? AND discord_id=?
                    """,
                    (platform, pseudo, str(discord_id)),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM accounts WHERE platform=? AND account_pseudo=?",
                    (platform, pseudo),
                ).fetchone()
        return self._row(row) if row else None

    def list_accounts(
        self,
        discord_id: str | None = None,
        config_name: str | None = None,
    ) -> list[StoredAccount]:
        with self._conn() as conn:
            if discord_id and config_name:
                cfg = self.normalize_config_name(config_name)
                rows = conn.execute(
                    """
                    SELECT * FROM accounts
                    WHERE discord_id=? AND config_name=?
                    ORDER BY platform, account_pseudo
                    """,
                    (str(discord_id), cfg),
                ).fetchall()
            elif discord_id:
                rows = conn.execute(
                    "SELECT * FROM accounts WHERE discord_id=? ORDER BY platform, account_pseudo",
                    (str(discord_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM accounts ORDER BY platform, account_pseudo"
                ).fetchall()
        return [self._row(r) for r in rows]

    def list_all_for_admin(self) -> list[StoredAccount]:
        return self.list_accounts(None)

    def delete_account(
        self,
        discord_id: str,
        platform: str,
        pseudo: str,
        config_name: str | None = None,
    ) -> bool:
        pseudo = pseudo.lower().lstrip("@")
        cfg = self.normalize_config_name(
            config_name or self.get_active_config_name(discord_id)
        )
        with self._conn() as conn:
            cur = conn.execute(
                """
                DELETE FROM accounts
                WHERE discord_id=? AND platform=? AND account_pseudo=? AND config_name=?
                """,
                (str(discord_id), platform.lower(), pseudo, cfg),
            )
            conn.commit()
            return cur.rowcount > 0

    def update_profile(
        self,
        discord_id: str,
        *,
        bio: str | None = None,
        bio_link: str | None = None,
        live_title: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sessions (discord_id, bio, bio_link, live_title, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    bio=COALESCE(excluded.bio, sessions.bio),
                    bio_link=COALESCE(excluded.bio_link, sessions.bio_link),
                    live_title=COALESCE(excluded.live_title, sessions.live_title),
                    updated_at=excluded.updated_at
                """,
                (
                    discord_id,
                    bio or "",
                    bio_link or "",
                    live_title or "Live",
                    _now(),
                ),
            )
            if bio is not None or bio_link is not None or live_title is not None:
                sets: list[str] = []
                vals: list[Any] = []
                if bio is not None:
                    sets.append("bio=?")
                    vals.append(bio)
                if bio_link is not None:
                    sets.append("bio_link=?")
                    vals.append(bio_link)
                if live_title is not None:
                    sets.append("live_title=?")
                    vals.append(live_title)
                sets.append("updated_at=?")
                vals.append(_now())
                vals.append(discord_id)
                conn.execute(
                    f"UPDATE accounts SET {', '.join(sets)} WHERE discord_id=?",
                    vals,
                )
            conn.commit()

    def get_session(self, discord_id: str) -> dict[str, Any]:
        active = self.get_active_config_name(discord_id)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE discord_id=?", (str(discord_id),)
            ).fetchone()
        if active != DEFAULT_CONFIG:
            profile = self.get_config_profile(discord_id, active)
            if profile:
                profile["active_config"] = active
                profile["discord_name"] = profile.get("discord_name") or (
                    row["discord_name"] if row else ""
                )
                return profile
        if not row:
            return {
                "discord_id": discord_id,
                "active_config": active,
                "game_key": "",
                "video_path": "",
                "live_title": "Live",
                "bio": "",
                "bio_link": "",
                "platforms": [],
                "overlay_config_name": "",
                "overlay_draft": {},
                "autolive_minutes": 14,
                "autolive_platforms": [],
                "bio_schedule_enabled": False,
            }
        draft: dict[str, Any] = {}
        raw_draft = row["overlay_draft_json"] if "overlay_draft_json" in row.keys() else ""
        if raw_draft:
            try:
                draft = json.loads(raw_draft)
            except json.JSONDecodeError:
                draft = {}
        return {
            "discord_id": row["discord_id"],
            "discord_name": row["discord_name"] or "",
            "active_config": active,
            "game_key": row["game_key"] or "",
            "video_path": row["video_path"] or "",
            "live_title": row["live_title"] or "Live",
            "bio": row["bio"] or "",
            "bio_link": row["bio_link"] or "",
            "platforms": json.loads(row["platforms_json"] or "[]"),
            "overlay_config_name": (row["overlay_config_name"] if "overlay_config_name" in row.keys() else "") or "",
            "overlay_draft": draft,
            "autolive_minutes": int(row["autolive_minutes"]) if "autolive_minutes" in row.keys() and row["autolive_minutes"] else 14,
            "autolive_platforms": json.loads(row["autolive_platforms_json"] or "[]")
            if "autolive_platforms_json" in row.keys()
            else [],
            "bio_schedule_enabled": bool(row["bio_schedule_enabled"])
            if "bio_schedule_enabled" in row.keys()
            else False,
        }

    def save_session(self, discord_id: str, discord_name: str, **fields: Any) -> None:
        active = self.get_active_config_name(discord_id)
        if active != DEFAULT_CONFIG:
            self.save_config_profile(discord_id, discord_name, active, **fields)
            self.set_active_config(discord_id, discord_name, active)
            return
        current = self.get_session(discord_id)
        current.update(fields)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    discord_id, discord_name, active_config, game_key, video_path, live_title,
                    bio, bio_link, platforms_json, overlay_config_name,
                    overlay_draft_json, autolive_minutes, autolive_platforms_json,
                    bio_schedule_enabled, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    discord_name=excluded.discord_name,
                    active_config=excluded.active_config,
                    game_key=excluded.game_key,
                    video_path=excluded.video_path,
                    live_title=excluded.live_title,
                    bio=excluded.bio,
                    bio_link=excluded.bio_link,
                    platforms_json=excluded.platforms_json,
                    overlay_config_name=excluded.overlay_config_name,
                    overlay_draft_json=excluded.overlay_draft_json,
                    autolive_minutes=excluded.autolive_minutes,
                    autolive_platforms_json=excluded.autolive_platforms_json,
                    bio_schedule_enabled=excluded.bio_schedule_enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    discord_id,
                    discord_name,
                    active,
                    current.get("game_key", ""),
                    current.get("video_path", ""),
                    current.get("live_title", "Live"),
                    current.get("bio", ""),
                    current.get("bio_link", ""),
                    json.dumps(current.get("platforms", [])),
                    current.get("overlay_config_name", ""),
                    json.dumps(current.get("overlay_draft", {}), ensure_ascii=False),
                    int(current.get("autolive_minutes", 14) or 14),
                    json.dumps(current.get("autolive_platforms", [])),
                    1 if current.get("bio_schedule_enabled") else 0,
                    _now(),
                ),
            )
            conn.commit()

    def create_license_key(
        self,
        *,
        key: str,
        dualhook: bool,
        duration: str,
        created_by: str,
    ) -> LicenseKey:
        expires_at: str | None = None
        now = datetime.now(timezone.utc)
        durations = {
            "1day": timedelta(days=1),
            "1week": timedelta(weeks=1),
            "1month": timedelta(days=30),
            "3months": timedelta(days=90),
        }
        if duration != "lifetime":
            delta = durations.get(duration)
            if not delta:
                raise ValueError(f"Duree invalide: {duration}")
            expires_at = (now + delta).isoformat()

        key = key.strip().upper()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO license_keys (key, dualhook, expires_at, created_by, created_at, revoked)
                VALUES (?,?,?,?,?,0)
                """,
                (key, 1 if dualhook else 0, expires_at, created_by, _now()),
            )
            conn.commit()
        return LicenseKey(
            key=key,
            dualhook=dualhook,
            expires_at=expires_at,
            created_by=created_by,
            created_at=_now(),
            revoked=False,
        )

    def get_license_key(self, key: str) -> LicenseKey | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT lk.*, ul.discord_id AS used_by_discord_id
                FROM license_keys lk
                LEFT JOIN user_licenses ul ON ul.license_key = lk.key
                WHERE lk.key = ?
                """,
                (key.strip().upper(),),
            ).fetchone()
        if not row:
            return None
        return LicenseKey(
            key=row["key"],
            dualhook=bool(row["dualhook"]),
            expires_at=row["expires_at"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            revoked=bool(row["revoked"]),
            used_by_discord_id=row["used_by_discord_id"],
        )

    def link_license(self, discord_id: str, key: str) -> tuple[bool, str]:
        key = key.strip().upper()
        lic = self.get_license_key(key)
        if not lic:
            return False, "Cle invalide."
        if lic.revoked:
            return False, "Cette cle a ete revoquee."
        if lic.expires_at:
            try:
                exp = datetime.fromisoformat(lic.expires_at)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) >= exp:
                    return False, "Cette cle est expiree."
            except ValueError:
                pass
        if lic.used_by_discord_id and lic.used_by_discord_id != str(discord_id):
            return False, "Cette cle est deja utilisee par un autre utilisateur."

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO user_licenses (discord_id, license_key, linked_at)
                VALUES (?,?,?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    license_key=excluded.license_key,
                    linked_at=excluded.linked_at
                """,
                (str(discord_id), key, _now()),
            )
            conn.commit()
        return True, "ok"

    def get_user_license(self, discord_id: str) -> UserLicense | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT ul.discord_id, ul.license_key, ul.linked_at,
                       lk.dualhook, lk.expires_at, lk.revoked
                FROM user_licenses ul
                JOIN license_keys lk ON lk.key = ul.license_key
                WHERE ul.discord_id = ?
                """,
                (str(discord_id),),
            ).fetchone()
        if not row:
            return None

        valid = True
        reason = ""
        if row["revoked"]:
            valid = False
            reason = "Cle revoquee."
        elif row["expires_at"]:
            try:
                exp = datetime.fromisoformat(row["expires_at"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) >= exp:
                    valid = False
                    reason = "Cle expiree."
            except ValueError:
                pass

        return UserLicense(
            discord_id=row["discord_id"],
            license_key=row["license_key"],
            dualhook=bool(row["dualhook"]),
            expires_at=row["expires_at"],
            linked_at=row["linked_at"],
            valid=valid,
            reason=reason,
        )

    def revoke_license_key(self, key: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE license_keys SET revoked=1 WHERE key=?",
                (key.strip().upper(),),
            )
            conn.commit()
            return cur.rowcount > 0

    def list_license_keys(self, limit: int = 50) -> list[LicenseKey]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT lk.*, ul.discord_id AS used_by_discord_id
                FROM license_keys lk
                LEFT JOIN user_licenses ul ON ul.license_key = lk.key
                ORDER BY lk.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            LicenseKey(
                key=r["key"],
                dualhook=bool(r["dualhook"]),
                expires_at=r["expires_at"],
                created_by=r["created_by"],
                created_at=r["created_at"],
                revoked=bool(r["revoked"]),
                used_by_discord_id=r["used_by_discord_id"],
            )
            for r in rows
        ]

    def _row(self, row: sqlite3.Row) -> StoredAccount:
        return StoredAccount(
            id=row["id"],
            discord_id=row["discord_id"],
            discord_name=row["discord_name"],
            platform=row["platform"],
            account_pseudo=row["account_pseudo"],
            cookies_json=row["cookies_json"] or "",
            cookie_hash=row["cookie_hash"] or "",
            stream_key=row["stream_key"] or "",
            rtmp_url=row["rtmp_url"] or "",
            full_push_url=row["full_push_url"] or "",
            room_id=row["room_id"] or "",
            bio=row["bio"] or "",
            bio_link=row["bio_link"] or "",
            original_bio_link=row["original_bio_link"] or ""
            if "original_bio_link" in row.keys()
            else "",
            live_title=row["live_title"] or "Live",
            updated_at=row["updated_at"] or "",
            config_name=(row["config_name"] if "config_name" in row.keys() else None)
            or DEFAULT_CONFIG,
        )

    def set_original_bio_link(
        self,
        discord_id: str,
        platform: str,
        pseudo: str,
        url: str,
    ) -> StoredAccount | None:
        return self.update_account_fields(
            str(discord_id),
            platform,
            pseudo,
            original_bio_link=url.strip(),
        )