"""Parse cookies TikTok — tous formats (JSON, Netscape, header, modal, export TXT)."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from urllib.parse import unquote

# Session live
SESSION_KEYS = frozenset({"sessionid", "sid_tt", "sessionid_ss"})
# Indices export TikTok valide
TIKTOK_HINTS = frozenset(
    {
        "sessionid",
        "sid_tt",
        "sessionid_ss",
        "sid_guard",
        "msToken",
        "ttwid",
        "odin_tt",
        "uid_tt",
        "passport_csrf_token",
        "s_v_web_id",
        "tt-target-idc",
        "tt-target-id",
        "store-country-code",
        "store-idc",
        "ssid_ucp_v1",
        "sid_ucp_v1",
        "multi_sids",
    }
)


@dataclass
class CookieParseResult:
    success: bool
    cookies: dict[str, str] | None = None
    error: str | None = None
    warning: str | None = None
    format_detected: str | None = None


def _clean_pairs(pairs: list[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in pairs:
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if not name or not value:
            continue
        if name.lower() in (
            "path", "domain", "expires", "max-age", "samesite", "secure", "httponly"
        ):
            continue
        if name not in out or len(value) > len(out[name]):
            out[name] = value
    return out


def _normalize_raw(raw: str) -> str:
    text = raw.strip().lstrip("\ufeff")
    # Export TXT style: COOKIE_HEADER: / COOKIE_JSON:
    m = re.search(
        r"(?:COOKIE_HEADER|Cookie\s*Header|header\s*string)\s*:\s*(.+?)(?:\n\s*\n|COOKIE_JSON|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1).strip()
    m2 = re.search(r"COOKIE_JSON\s*:\s*(\{.+\})", text, re.IGNORECASE | re.DOTALL)
    if m2:
        return m2.group(1).strip()
    return text


def _session_from_sid_guard(cookies: dict[str, str]) -> str:
    guard = (cookies.get("sid_guard") or "").strip()
    if not guard:
        return ""
    return unquote(guard).split("|")[0].strip()


def sanitize_cookies(cookies: dict[str, str]) -> dict[str, str]:
    """Nettoie cles corrompues (JSON double-colle) et complete sessionid."""
    out: dict[str, str] = {}
    for k, v in cookies.items():
        key = str(k).strip()
        val = str(v).strip() if v is not None else ""
        if not key:
            continue
        low = key.lower()
        if low.startswith("cookie:"):
            nested = _parse_header_style(key.split(":", 1)[1] if ":" in key else key)
            out.update(nested)
            continue
        if key.startswith("{"):
            for attempt in (key, key + "}", key + val if val else key):
                try:
                    data = json.loads(attempt)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    for nk, nv in data.items():
                        if nv is not None and not isinstance(nv, (dict, list)):
                            out[str(nk)] = str(nv)
                    break
            continue
        if low in ("path", "domain", "expires", "max-age", "samesite", "secure"):
            continue
        if val:
            out[key] = val

    sid = _session_from_sid_guard(out)
    if len(sid) >= 8:
        for sk in ("sessionid", "sid_tt", "sessionid_ss"):
            if not (out.get(sk) or "").strip():
                out[sk] = sid
    return out


def _item_name_value(item: dict) -> tuple[str, str] | None:
    name = (
        item.get("name")
        or item.get("Name")
        or item.get("key")
        or item.get("Key")
        or item.get("cookie_name")
    )
    value = (
        item.get("value")
        or item.get("Value")
        or item.get("cookie_value")
        or item.get("val")
    )
    if name and value is not None:
        return str(name), str(value)
    return None


def _pairs_from_json_value(data: object, depth: int = 0) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if depth > 6 or data is None:
        return pairs
    if isinstance(data, str):
        text = data.strip()
        if text.startswith(("{", "[")):
            try:
                return _pairs_from_json_value(json.loads(text), depth + 1)
            except json.JSONDecodeError:
                pass
        if "=" in text:
            return list(_parse_header_style(text).items())
        return pairs
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                nv = _item_name_value(item)
                if nv:
                    pairs.append(nv)
                else:
                    pairs.extend(_pairs_from_json_value(item, depth + 1))
            elif isinstance(item, str) and "=" in item:
                pairs.extend(_parse_header_style(item).items())
        return pairs
    if isinstance(data, dict):
        # Cookie-Editor flat object or nested cookies key
        if any(data.get(k) for k in SESSION_KEYS | TIKTOK_HINTS):
            for k, v in data.items():
                if v is not None and not isinstance(v, (dict, list)):
                    pairs.append((str(k), str(v)))
            return pairs
        for key in (
            "cookies", "cookie", "Cookie", "data", "headers", "header",
            "input", "raw", "value", "content", "payload", "account",
        ):
            if key in data:
                nested = _pairs_from_json_value(data[key], depth + 1)
                if nested:
                    return nested
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                nested = _pairs_from_json_value(v, depth + 1)
                if nested:
                    return nested
            elif v is not None:
                pairs.append((str(k), str(v)))
    return pairs


def _parse_json_object(raw: str) -> dict[str, str] | None:
    text = _normalize_raw(raw)
    if text.endswith(",") and text.startswith("["):
        text = text.rstrip(",").rstrip() + "]"
    for attempt in (text, text.replace("'", '"')):
        try:
            data = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        pairs = _pairs_from_json_value(data)
        if pairs:
            return _clean_pairs(pairs)
    m = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            pairs = _pairs_from_json_value(data)
            return _clean_pairs(pairs) if pairs else None
        except json.JSONDecodeError:
            pass
    return None


def _parse_netscape(raw: str) -> dict[str, str] | None:
    pairs: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if "\t" in line:
            parts = line.split("\t")
            if len(parts) >= 7:
                pairs.append((parts[5], parts[6]))
                continue
        if "=" in line:
            n, _, v = line.partition("=")
            pairs.append((n, v))
    return _clean_pairs(pairs) if pairs else None


def _parse_header_style(raw: str) -> dict[str, str]:
    text = raw.strip()
    for prefix in ("cookie:", "set-cookie:", "cookies:"):
        if text.lower().startswith(prefix):
            text = text.split(":", 1)[1].strip()
            break
    pairs: list[tuple[str, str]] = []
    normalized = text.replace("\n", ";").replace("\r", ";")
    for part in re.split(r"[;]+", normalized):
        part = part.strip()
        if not part or "=" not in part:
            continue
        n, _, v = part.partition("=")
        pairs.append((n, v))
    return _clean_pairs(pairs)


def _try_base64(raw: str) -> dict[str, str] | None:
    text = raw.strip()
    if len(text) < 20 or " " in text[:40]:
        return None
    try:
        # padding
        pad = (-len(text)) % 4
        decoded = base64.b64decode(text + ("=" * pad)).decode("utf-8", errors="ignore")
    except Exception:
        return None
    if not decoded or ("=" not in decoded and "{" not in decoded):
        return None
    return parse_cookies(decoded).cookies if parse_cookies(decoded).success else None


def _validate_cookies(cookies: dict[str, str]) -> tuple[bool, str | None, str | None]:
    if not cookies:
        return False, "Aucun cookie trouve.", None
    names = set(cookies.keys())
    if names & SESSION_KEYS:
        return True, None, None
    if names & TIKTOK_HINTS:
        return (
            True,
            None,
            "Pas de sessionid/sid_tt — cle RTMP tentee (sid_guard/msToken detectes).",
        )
    if len(cookies) >= 2:
        return True, None, "Format accepte — verification session au moment de la cle RTMP."
    return False, "Cookies trop courts. Colle un export complet (JSON, Netscape ou header).", None


def split_cookie_blocks(raw: str) -> list[str]:
    """
    Separe plusieurs cookies colles d'affilee (sans fichier).
    Separateurs: --- / === / ### / ACCOUNT: / ---compte--- / double saut de ligne + JSON.
    """
    text = (raw or "").strip().lstrip("\ufeff")
    if not text:
        return []

    # Separateurs explicites
    parts = re.split(
        r"\n\s*(?:-{3,}|={3,}|#{3,})\s*\n"
        r"|\n\s*(?:ACCOUNT|COMPTE|COOKIE)\s*[:=#]?\s*\n"
        r"|\n\s*@{1,2}[a-zA-Z0-9._]+\s*\n",
        text,
        flags=re.IGNORECASE,
    )
    blocks = [p.strip() for p in parts if p and p.strip()]

    if len(blocks) <= 1:
        # Plusieurs objets JSON colles : }{  ou ][
        multi_json = re.findall(r"(\[[\s\S]*?\]|\{[\s\S]*?\})(?=\s*(?:\[|\{|$))", text)
        if len(multi_json) > 1:
            blocks = [b.strip() for b in multi_json if b.strip()]

    if len(blocks) <= 1:
        # Blocs separes par 2+ lignes vides
        by_blank = re.split(r"\n\s*\n\s*\n+", text)
        by_blank = [b.strip() for b in by_blank if b.strip()]
        if len(by_blank) > 1:
            blocks = by_blank

    return blocks if blocks else [text]


def parse_multiple_cookies(raw: str) -> tuple[list[dict[str, str]], list[str]]:
    """Parse 1..N blocs cookies. Retourne (liste_cookies_ok, erreurs)."""
    ok: list[dict[str, str]] = []
    errors: list[str] = []
    blocks = split_cookie_blocks(raw)
    for i, block in enumerate(blocks, 1):
        # Ignore pseudo-only lines
        if re.fullmatch(r"@?[a-zA-Z0-9._]{2,40}", block.strip()):
            continue
        parsed = parse_cookies(block)
        if parsed.success and parsed.cookies:
            ok.append(parsed.cookies)
        else:
            errors.append(f"Bloc {i}: {parsed.error or 'invalide'}")
    return ok, errors


def parse_cookies(raw: str, *, strict: bool = False) -> CookieParseResult:
    """
    Formats acceptes:
    - JSON Cookie-Editor / EditThisCookie [{name,value}]
    - JSON objet {sessionid:...}
    - Netscape
    - Header name=value; name2=value2
    - COOKIE_HEADER: / COOKIE_JSON: (exports Fazox)
    - Lignes name=value
    - Base64
    """
    text = _normalize_raw(raw)
    if not text:
        return CookieParseResult(success=False, error="Colle tes cookies.")

    cookies: dict[str, str] | None = None
    fmt: str | None = None

    if text.startswith("{") or text.startswith("["):
        cookies = _parse_json_object(text)
        if cookies:
            fmt = "json"

    if not cookies and ("# Netscape" in text or "\t" in text):
        cookies = _parse_netscape(text)
        if cookies:
            fmt = "netscape"

    if not cookies:
        cookies = _parse_header_style(text)
        if cookies:
            fmt = "header"

    if not cookies:
        line_cookies = _parse_netscape(text)
        if line_cookies:
            cookies = line_cookies
            fmt = "lines"

    if not cookies:
        cookies = _try_base64(text)
        if cookies:
            fmt = "base64"

    if not cookies:
        return CookieParseResult(
            success=False,
            error=(
                "Format non reconnu. Accepte:\n"
                "• sessionid=xxx; sid_tt=yyy\n"
                "• JSON Cookie-Editor\n"
                "• COOKIE_HEADER: ...\n"
                "• Netscape / .txt export"
            ),
        )

    cookies = sanitize_cookies(cookies)
    ok, err, warning = _validate_cookies(cookies)
    if not ok:
        if strict:
            return CookieParseResult(success=False, error=err)
        if len(cookies) >= 1:
            return CookieParseResult(
                success=True,
                cookies=cookies,
                warning=err or "Format minimal accepte.",
                format_detected=fmt,
            )
        return CookieParseResult(success=False, error=err)

    return CookieParseResult(
        success=True, cookies=cookies, warning=warning, format_detected=fmt
    )
