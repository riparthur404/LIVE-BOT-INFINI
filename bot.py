"""Fazox Multi Live Bot — TikTok, Twitch, YouTube, Kick + recs + webhook + VIP admin."""

from __future__ import annotations

import io
import json
import logging
import os
import runpy
import sys
from pathlib import Path
from typing import Optional

_BOT_FILE = Path(__file__).resolve()


def _is_pebble_entry() -> bool:
    """PebbleHost charge bot.py via import (__name__ != __main__)."""
    if __name__ == "__main__":
        return True
    main = sys.modules.get("__main__")
    if main is None:
        return False
    main_file = getattr(main, "__file__", None)
    if not main_file:
        return False
    return Path(main_file).resolve() == _BOT_FILE


# Bot Start File = bot.py dans le panel → redirige vers start.py (logs + erreurs).
if _is_pebble_entry():
    _entry = _BOT_FILE.parent / "start.py"
    if _entry.is_file():
        runpy.run_path(str(_entry), run_name="__main__")
        raise SystemExit(0)
    print("ERREUR: start.py introuvable — upload tout fazox-pebblehost.zip", flush=True)
    raise SystemExit(1)

print("[Fazox] bot.py chargement...", flush=True)

_ROOT = Path(__file__).resolve().parent
_REQUIRED = (
    "start.py",
    "public_commands.py",
    "autolive.py",
    "bio_scheduler.py",
    "live_engine.py",
    "tiktok_live.py",
    "config.py",
    "database.py",
    "requirements.txt",
)


def _check_deploy() -> None:
    missing = [p for p in _REQUIRED if not (_ROOT / p).is_file()]
    if missing:
        print("=" * 60)
        print("ERREUR DEPLOIEMENT — fichiers manquants sur le serveur:")
        for p in missing:
            print(f"  - {p}")
        print()
        print("Tu as probablement upload seulement bot.py.")
        print("Sur PebbleHost: upload et EXTRAIS tout fazox-pebblehost.zip")
        print("dans /home/container/ (pas dans un sous-dossier).")
        print("Fichiers requis a la racine: start.py + bot.py + tiktok_live.py + live_engine.py")
        print("Startup PebbleHost: python3 -u start.py")
        print("=" * 60)
        sys.exit(1)


_check_deploy()

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from config import DISCORD_TOKEN, PLATFORM_LABELS, RECS_DIR, VIDEOS_DIR
from overlay_editor import open_custom_editor, register_custom_command
from public_commands import register_public_commands
from cookie_parser import parse_cookies
from database import Database
from overlay_config import (
    ImageOverlay,
    OverlayConfig,
    TextOverlay,
    delete_config,
    draft_from_session,
    draft_to_session_dict,
    list_configs,
    load_config,
    save_config,
    user_assets_dir,
)
from overlay_render import overlay_png_path, preview_path, render_overlay_png, render_preview
from live_engine import (
    apply_profile_live,
    db,
    format_share_links,
    get_viewers,
    refresh_viewers,
    save_manual_key,
    save_tiktok_cookies,
    start_live,
    state,
    stop_live,
    list_tiktok_accounts,
    update_all_tiktok_bio_links,
    update_link_via_cookie,
)
from rec_library import game_choices_for_discord, list_games, resolve_rec, resolve_video_path
from license import (
    DURATION_LABELS,
    TYPE_LABELS,
    format_expiry,
    generate_key,
    is_admin,
    is_vip,
    license_status_text,
    user_dualhook_enabled,
    user_has_valid_license,
)

load_dotenv()
log = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


LICENSE_EXEMPT = {
    "aide",
    "cle",
    "live admin key_generate",
    "live admin key_list",
    "live admin key_revoke",
    "live logs",
    "live tous",
}


def user_name(interaction: discord.Interaction) -> str:
    return interaction.user.display_name or str(interaction.user)


async def _send_live_links_mp(user: discord.abc.User) -> str | None:
    from live_engine import get_share_urls_map

    share_map = get_share_urls_map(str(user.id))
    if not share_map:
        links = format_share_links(str(user.id))
        if not links:
            return None
        embed = discord.Embed(title="🔗 Liens de ton live", description=links, color=0xFE2C55)
        try:
            await user.send(embed=embed)
            return "📩 Liens du live envoyes en MP."
        except discord.Forbidden:
            return f"⚠️ Tes MP sont fermes — voici les liens:\n{links}"
        except Exception:
            return f"⚠️ Envoi MP echoue — voici les liens:\n{links}"

    sent = 0
    fallback_lines: list[str] = []
    try:
        summary = discord.Embed(
            title="🔗 Tes lives sont en ligne",
            description=f"**{len(share_map)}** lien(s) ci-dessous — 1 MP par live.",
            color=0xFE2C55,
        )
        await user.send(embed=summary)
        sent += 1
        for key, url in share_map.items():
            if key.startswith("tiktok:"):
                pseudo = key.split(":", 1)[1]
                title = f"🎵 TikTok @{pseudo}"
            else:
                title = f"📡 Live {key}"
            embed = discord.Embed(
                title=title,
                description=f"**Lien direct:**\n{url}",
                color=0xFE2C55,
            )
            embed.add_field(name="Ouvrir", value=url, inline=False)
            await user.send(embed=embed)
            sent += 1
            fallback_lines.append(f"**{title}**\n{url}")
        return f"📩 **{sent}** message(s) MP envoyes (1 par live)."
    except discord.Forbidden:
        return "⚠️ Tes MP sont fermes — ouvre-les pour recevoir chaque lien.\n\n" + "\n\n".join(
            fallback_lines
        )
    except Exception:
        return "⚠️ Envoi MP echoue — voici les liens:\n\n" + "\n\n".join(fallback_lines)


async def _reply_live_result(
    interaction: discord.Interaction, ok: bool, msg: str
) -> None:
    extra = ""
    if ok:
        hint = await _send_live_links_mp(interaction.user)
        if hint:
            extra = f"\n\n{hint}"
    color = 0x57F287 if ok else 0xED4245
    title = "Live lance" if ok else "Echec live"
    embed = discord.Embed(
        title=f"{'✅' if ok else '❌'} {title}",
        description=f"{msg}{extra}"[:4096],
        color=color,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _require_license(interaction: discord.Interaction) -> bool:
    """Retourne True si l'utilisateur peut continuer (interaction_check gere deja le refus)."""
    uid = str(interaction.user.id)
    if user_has_valid_license(uid) or is_admin(interaction):
        return True
    if interaction.response.is_done():
        return False
    await interaction.response.send_message(
        "❌ Cle de licence requise. `/cle cle:FAZOX-...`\n"
        "Demande une cle a un administrateur si tu n'en as pas.",
        ephemeral=True,
    )
    return False


# ─── UI ───────────────────────────────────────────────────────────────────────

class GameSelect(discord.ui.Select):
    def __init__(self) -> None:
        choices = game_choices_for_discord()
        if not choices:
            choices = [("Aucune video — mets des .mp4 dans recs/", "_none")]
        options = [
            discord.SelectOption(label=l[:100], value=v) for l, v in choices[:25]
        ]
        super().__init__(placeholder="Choisir le jeu / rec...", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _require_license(interaction):
            return
        if self.values[0] == "_none":
            await interaction.response.send_message("Ajoute des .mp4 dans `recs/`", ephemeral=True)
            return
        path = resolve_rec(self.values[0])
        if not path:
            await interaction.response.send_message(
                f"❌ Rec `{self.values[0]}` introuvable dans `{RECS_DIR}`", ephemeral=True
            )
            return
        db.save_session(
            str(interaction.user.id), user_name(interaction), game_key=self.values[0], video_path=str(path)
        )
        await interaction.response.send_message(
            f"✅ Rec **{self.values[0]}** → `{path.name}`", ephemeral=True
        )


class PlatformSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label=PLATFORM_LABELS[p], value=p, emoji="📡")
            for p in ("tiktok", "twitch", "youtube", "kick")
        ]
        super().__init__(
            placeholder="Plateformes (multi-sélection)...",
            options=options,
            min_values=1,
            max_values=4,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _require_license(interaction):
            return
        db.save_session(
            str(interaction.user.id), user_name(interaction), platforms=self.values
        )
        names = ", ".join(PLATFORM_LABELS.get(p, p) for p in self.values)
        await interaction.response.send_message(f"✅ Plateformes: **{names}**", ephemeral=True)


class VipAllLiveModal(discord.ui.Modal, title="Live tous les comptes (VIP)"):
    vipkey = discord.ui.TextInput(label="Clé VIP", placeholder="Fazox1234", required=True)

    def __init__(self, discord_id: str, discord_name: str) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_vip(self.vipkey.value):
            await interaction.response.send_message("❌ Clé VIP invalide.", ephemeral=True)
            return
        sess = db.get_session(self.discord_id)
        vpath = resolve_video_path(sess.get("video_path"))
        if not vpath:
            await interaction.response.send_message("❌ Aucune video disponible.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok, msg = await start_live(
            self.discord_id,
            vpath,
            [],
            sess.get("live_title") or "Live",
            use_all_stock=True,
        )
        await _reply_live_result(interaction, ok, msg)


class LivePanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=600)
        self.add_item(GameSelect())
        self.add_item(PlatformSelect())

    @discord.ui.button(label="🌍 Tous les comptes", style=discord.ButtonStyle.primary, row=2)
    async def all_stock(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            VipAllLiveModal(str(interaction.user.id), user_name(interaction))
        )

    @discord.ui.button(label="🚀 Lancer le live", style=discord.ButtonStyle.success, row=2)
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _require_license(interaction):
            return
        sess = db.get_session(str(interaction.user.id))
        vpath = resolve_video_path(sess.get("video_path"))
        if not vpath:
            await interaction.response.send_message(
                "❌ Aucune video disponible. Ajoute des .mp4 dans `recs/` ou choisis un jeu.", ephemeral=True
            )
            return
        platforms = sess.get("platforms") or ["tiktok"]
        title = sess.get("live_title") or "Live"
        await interaction.response.defer(ephemeral=True)
        ok, msg = await start_live(
            str(interaction.user.id), vpath, platforms, title
        )
        await _reply_live_result(interaction, ok, msg)

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, row=3)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await _require_license(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await stop_live(str(interaction.user.id))
        await interaction.followup.send("⏹️ Live arrêté.", ephemeral=True)


class ChangeLinkModal(discord.ui.Modal, title="Nouveau lien bio TikTok"):
    url = discord.ui.TextInput(
        label="Nouveau lien bio",
        placeholder="https://discord.gg/... ou https://...",
        required=True,
    )

    def __init__(self, discord_id: str, discord_name: str, pseudo: str) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name
        self.pseudo = pseudo

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await update_link_via_cookie(
            self.discord_id,
            self.discord_name,
            self.url.value.strip(),
            self.pseudo,
        )
        await interaction.followup.send(f"{'✅' if ok else '❌'} {msg}", ephemeral=True)


class ChangeAllLinksModal(discord.ui.Modal, title="Lien bio — TOUS les comptes TikTok"):
    url = discord.ui.TextInput(
        label="Nouveau lien pour tous les comptes",
        placeholder="https://...",
        required=True,
    )

    def __init__(self, discord_id: str, discord_name: str) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await update_all_tiktok_bio_links(
            self.discord_id, self.discord_name, self.url.value.strip()
        )
        await interaction.followup.send(f"{'✅' if ok else '❌'} {msg}", ephemeral=True)


class TikTokAccountSelect(discord.ui.Select):
    def __init__(self, accounts: list) -> None:
        options = [
            discord.SelectOption(
                label=f"@{a.account_pseudo}"[:100],
                description=(a.bio_link or "Pas de lien")[:100],
                value=a.account_pseudo,
            )
            for a in accounts[:25]
        ]
        super().__init__(
            placeholder="Choisir le compte TikTok...",
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: LienBioPanel = self.view  # type: ignore[assignment]
        view.selected_pseudo = self.values[0]
        acc = db.get_account("tiktok", self.values[0])
        link = acc.bio_link if acc else "—"
        await interaction.response.send_message(
            f"Compte **@{self.values[0]}** selectionne.\nLien actuel: {link or '—'}",
            ephemeral=True,
        )


class LienBioPanel(discord.ui.View):
    def __init__(self, discord_id: str, discord_name: str, accounts: list) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name
        self.selected_pseudo = accounts[0].account_pseudo if accounts else ""
        self.add_item(TikTokAccountSelect(accounts))

    @discord.ui.button(label="🔗 Modifier ce compte", style=discord.ButtonStyle.primary, row=1)
    async def change_one(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.selected_pseudo:
            await interaction.response.send_message(
                "Selectionne un compte TikTok d'abord.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            ChangeLinkModal(self.discord_id, self.discord_name, self.selected_pseudo)
        )

    @discord.ui.button(label="🌍 Tous les comptes", style=discord.ButtonStyle.secondary, row=1)
    async def change_all(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            ChangeAllLinksModal(self.discord_id, self.discord_name)
        )


class LienCookieModal(discord.ui.Modal, title="Nouveau lien via cookie"):
    def __init__(self, discord_id: str, discord_name: str) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name
        self.pseudo = discord.ui.TextInput(label="Pseudo TikTok", placeholder="monpseudo", required=True)
        self.url = discord.ui.TextInput(label="Nouveau lien bio", placeholder="https://...", required=True)
        self.cookies = discord.ui.TextInput(
            label="Cookies JSON (optionnel si deja enregistre)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000,
        )
        self.add_item(self.pseudo)
        self.add_item(self.url)
        self.add_item(self.cookies)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        cookies_dict = None
        if self.cookies.value.strip():
            parsed = parse_cookies(self.cookies.value)
            if not parsed.success or not parsed.cookies:
                await interaction.followup.send(f"❌ {parsed.error}", ephemeral=True)
                return
            cookies_dict = parsed.cookies
        ok, msg = await update_link_via_cookie(
            self.discord_id,
            self.discord_name,
            self.url.value.strip(),
            self.pseudo.value.strip().lstrip("@"),
            cookies=cookies_dict,
        )
        await interaction.followup.send(f"{'✅' if ok else '❌'} {msg}", ephemeral=True)


def _active_overlay_config(discord_id: str) -> OverlayConfig:
    sess = db.get_session(discord_id)
    name = (sess.get("overlay_config_name") or "").strip()
    if name:
        loaded = load_config(discord_id, name)
        if loaded:
            return loaded
    return draft_from_session(sess)


def _save_draft(discord_id: str, discord_name: str, config: OverlayConfig) -> None:
    db.save_session(discord_id, discord_name, **draft_to_session_dict(config))


async def _send_public_preview(
    interaction: discord.Interaction,
    discord_id: str,
    discord_name: str,
    cfg: OverlayConfig,
) -> None:
    sess = db.get_session(discord_id)
    video = sess.get("video_path") or ""
    vpath = Path(video) if video and Path(video).is_file() else None
    out = preview_path(discord_id, cfg.name or "preview")
    render_preview(cfg, discord_id, vpath, out)

    embed = discord.Embed(
        title="🎨 Aperçu du live",
        description=(
            f"Config: **{cfg.name or 'brouillon'}**\n"
            f"Par **{discord_name}**\n\n"
            "**Ça te convient ?**"
        ),
        color=0x9B59B6,
    )
    embed.set_image(url="attachment://apercu_live.jpg")
    embed.set_footer(text="Visible par tout le salon — confirme ou modifie ta config")

    await interaction.followup.send(
        embed=embed,
        file=discord.File(out, filename="apercu_live.jpg"),
        view=PreviewConfirmView(discord_id, discord_name),
    )


class PreviewConfirmView(discord.ui.View):
    def __init__(self, discord_id: str, discord_name: str) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name

    async def _only_owner(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.discord_id:
            await interaction.response.send_message(
                "Seul celui qui a créé cette config peut confirmer.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Oui, ça me convient", style=discord.ButtonStyle.success, row=0)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._only_owner(interaction):
            return
        await interaction.response.send_message(
            "✅ **Parfait !** Clique **💾 Sauvegarder** sur le panneau ou `/live overlay save nom:...` "
            "puis lance `/live start`.",
            ephemeral=True,
        )

    @discord.ui.button(label="❌ Non, je modifie", style=discord.ButtonStyle.danger, row=0)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._only_owner(interaction):
            return
        await interaction.response.send_message(
            "📝 Modifie avec `/live overlay text` ou `/live overlay image`, "
            "puis reclique **👁️ Aperçu visuel**.",
            ephemeral=True,
        )


class OverlaySaveModal(discord.ui.Modal, title="Sauvegarder la config overlay"):
    nom = discord.ui.TextInput(label="Nom de la config", placeholder="ex: promo robux", required=True, max_length=40)

    def __init__(self, discord_id: str, discord_name: str) -> None:
        super().__init__(timeout=300)
        self.discord_id = discord_id
        self.discord_name = discord_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg = _active_overlay_config(self.discord_id)
        if not cfg.texts and not cfg.images:
            await interaction.response.send_message(
                "❌ Rien à sauvegarder. Ajoute du texte ou une image d'abord.", ephemeral=True
            )
            return
        cfg.name = self.nom.value.strip()
        save_config(self.discord_id, cfg)
        db.save_session(
            self.discord_id,
            self.discord_name,
            overlay_config_name=cfg.name,
            overlay_draft=cfg.to_dict(),
        )
        await interaction.response.send_message(
            f"✅ Config **{cfg.name}** sauvegardée.\nUtilise `/live overlay load nom:{cfg.name}` pour la réactiver.",
            ephemeral=True,
        )


class OverlayPanel(discord.ui.View):
    def __init__(self, discord_id: str, discord_name: str) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name

    async def _only_owner(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.discord_id:
            await interaction.response.send_message(
                "Ce panneau appartient à quelqu'un d'autre.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="👁️ Aperçu visuel", style=discord.ButtonStyle.primary, row=0)
    async def preview(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._only_owner(interaction):
            return
        cfg = _active_overlay_config(self.discord_id)
        if not cfg.texts and not cfg.images:
            await interaction.response.send_message(
                "❌ Aucun texte/image. Ajoute un overlay d'abord avec `/live overlay text` ou `image`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await _send_public_preview(interaction, self.discord_id, self.discord_name, cfg)

    @discord.ui.button(label="💾 Sauvegarder", style=discord.ButtonStyle.success, row=0)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._only_owner(interaction):
            return
        await interaction.response.send_modal(OverlaySaveModal(self.discord_id, self.discord_name))

    @discord.ui.button(label="🗑️ Vider brouillon", style=discord.ButtonStyle.secondary, row=1)
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._only_owner(interaction):
            return
        db.save_session(
            self.discord_id,
            self.discord_name,
            overlay_config_name="",
            overlay_draft={},
        )
        await interaction.response.send_message("✅ Brouillon overlay effacé.", ephemeral=True)


class CookieModal(discord.ui.Modal, title="Cookies TikTok"):
    def __init__(self) -> None:
        super().__init__(timeout=600)
        self.pseudo = discord.ui.TextInput(label="Pseudo TikTok", placeholder="monpseudo", required=True)
        self.cookies = discord.ui.TextInput(
            label="Cookies JSON", style=discord.TextStyle.paragraph, required=True, max_length=4000
        )
        self.add_item(self.pseudo)
        self.add_item(self.cookies)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_license(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        parsed = parse_cookies(self.cookies.value)
        if not parsed.success or not parsed.cookies:
            await interaction.followup.send(f"❌ {parsed.error}", ephemeral=True)
            return
        sess = db.get_session(str(interaction.user.id))
        dualhook = user_dualhook_enabled(str(interaction.user.id))
        acc, err = await save_tiktok_cookies(
            str(interaction.user.id),
            user_name(interaction),
            self.pseudo.value.strip().lstrip("@"),
            parsed.cookies,
            title=sess.get("live_title") or "Live",
            bio=sess.get("bio") or "",
            bio_link=sess.get("bio_link") or "",
        )
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return
        wh = "webhook dualhook actif" if dualhook else "pas de webhook (cle sans dualhook)"
        await interaction.followup.send(
            f"✅ TikTok **@{acc.account_pseudo}** enregistre en DB.\n"
            f"Proprietaire Discord: **{user_name(interaction)}** (`{interaction.user.id}`)\n"
            f"Cookie + cle sauvegardes — {wh}\n"
            f"Utilise `/live lien_cookie` pour changer le lien bio.",
            ephemeral=True,
        )


# ─── Commands ─────────────────────────────────────────────────────────────────

live = app_commands.Group(name="live", description="🎬 Fazox Multi Live")
admin = app_commands.Group(name="admin", description="🛡️ [Admin] Gestion des cles", parent=live)
overlay = app_commands.Group(name="overlay", description="🎨 Texte + images sur la video live", parent=live)


@admin.command(name="key_generate", description="🔑 Generer une cle de licence")
@app_commands.describe(
    type_cle="dualhook = webhook actif | normal = pas de webhook",
    duree="Duree de validite de la cle",
)
@app_commands.choices(
    type_cle=[
        app_commands.Choice(name="Dualhook (webhook)", value="dualhook"),
        app_commands.Choice(name="Sans dualhook", value="normal"),
    ],
    duree=[
        app_commands.Choice(name="Lifetime — n'expire jamais", value="lifetime"),
        app_commands.Choice(name="1 jour", value="1day"),
        app_commands.Choice(name="1 semaine", value="1week"),
        app_commands.Choice(name="1 mois", value="1month"),
        app_commands.Choice(name="3 mois", value="3months"),
    ],
)
async def live_key_generate(
    interaction: discord.Interaction,
    type_cle: app_commands.Choice[str],
    duree: app_commands.Choice[str],
) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Reserve aux administrateurs.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    key = generate_key()
    dualhook = type_cle.value == "dualhook"
    lic = db.create_license_key(
        key=key,
        dualhook=dualhook,
        duration=duree.value,
        created_by=str(interaction.user.id),
    )
    await interaction.followup.send(
        f"✅ Cle generee !\n"
        f"```\n{lic.key}\n```\n"
        f"**Type:** {TYPE_LABELS['dualhook' if dualhook else 'normal']}\n"
        f"**Duree:** {DURATION_LABELS.get(duree.value, duree.value)}\n"
        f"**Expire:** {format_expiry(lic.expires_at)}\n\n"
        f"Donne cette cle a l'utilisateur — il l'active avec `/cle cle:{lic.key}`",
        ephemeral=True,
    )


@admin.command(name="key_list", description="📋 Lister les cles generees")
async def live_key_list(interaction: discord.Interaction) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Reserve aux administrateurs.", ephemeral=True)
        return
    keys = db.list_license_keys(limit=30)
    if not keys:
        await interaction.response.send_message("Aucune cle generee.", ephemeral=True)
        return
    lines = []
    for k in keys:
        status = "revoquee" if k.revoked else ("utilisee" if k.used_by_discord_id else "libre")
        kind = "dualhook" if k.dualhook else "normal"
        exp = format_expiry(k.expires_at)
        user = f" → <@{k.used_by_discord_id}>" if k.used_by_discord_id else ""
        lines.append(f"`{k.key}` — {kind} — {exp} — **{status}**{user}")
    await interaction.response.send_message(
        f"**Cles de licence** ({len(keys)})\n" + "\n".join(lines[:25]),
        ephemeral=True,
    )


@admin.command(name="key_revoke", description="🚫 Revoquer une cle")
@app_commands.describe(cle="Cle a revoquer")
async def live_key_revoke(interaction: discord.Interaction, cle: str) -> None:
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Reserve aux administrateurs.", ephemeral=True)
        return
    if not db.revoke_license_key(cle):
        await interaction.response.send_message(f"❌ Cle `{cle}` introuvable.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Cle `{cle.strip().upper()}` revoquee.", ephemeral=True)


@live.command(name="logs", description="📋 [VIP] Tous les cookies et cles en stock")
@app_commands.describe(vipkey="Clé VIP Fazox1234")
async def live_logs(interaction: discord.Interaction, vipkey: str) -> None:
    if not is_vip(vipkey):
        await interaction.response.send_message("❌ Clé VIP invalide.", ephemeral=True)
        return
    accs = db.list_all_for_admin()
    if not accs:
        await interaction.response.send_message("Stock vide.", ephemeral=True)
        return
    by_platform: dict[str, int] = {}
    export: list[dict] = []
    for a in accs:
        by_platform[a.platform] = by_platform.get(a.platform, 0) + 1
        export.append({
            "platform": a.platform,
            "pseudo": a.account_pseudo,
            "discord_name": a.discord_name,
            "discord_id": a.discord_id,
            "stream_key": a.stream_key,
            "rtmp_url": a.rtmp_url,
            "full_push_url": a.full_push_url,
            "room_id": a.room_id,
            "cookies": json.loads(a.cookies_json) if a.cookies_json else None,
            "updated_at": a.updated_at,
        })

    summary = ", ".join(f"{p.upper()}: {n}" for p, n in sorted(by_platform.items()))
    embed = discord.Embed(
        title="📋 Stock comptes (VIP)",
        description=f"**{len(accs)} comptes** — {summary}",
        color=0xFF0000,
    )
    for a in accs[:12]:
        key = a.stream_key[:80] + "..." if len(a.stream_key) > 80 else (a.stream_key or "—")
        embed.add_field(
            name=f"{a.platform.upper()} @{a.account_pseudo}",
            value=f"Discord: **{a.discord_name}** (`{a.discord_id}`)\nClé: `{key}`",
            inline=False,
        )
    if len(accs) > 12:
        embed.set_footer(text=f"+ {len(accs) - 12} autres — export JSON joint")

    payload = json.dumps(export, indent=2, ensure_ascii=False).encode("utf-8")
    file = discord.File(io.BytesIO(payload), filename="fazox_stock.json")
    await interaction.response.send_message(embed=embed, file=file, ephemeral=True)


@live.command(name="tous", description="🌍 [VIP] Live sur TOUS les tokens/comptes en stock")
@app_commands.describe(vipkey="Clé VIP Fazox1234", jeu="Jeu/rec ex: adopt me (optionnel)")
async def live_tous(
    interaction: discord.Interaction, vipkey: str, jeu: Optional[str] = None
) -> None:
    if not is_vip(vipkey):
        await interaction.response.send_message("❌ Clé VIP invalide.", ephemeral=True)
        return
    sess = db.get_session(str(interaction.user.id))
    path = resolve_rec(jeu) if jeu else None
    if not path:
        path = resolve_video_path(sess.get("video_path"))
    if not path:
        await interaction.response.send_message(
            "❌ Choisis un rec: `/live jeu nom:adopt me` ou paramètre `jeu:`", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    ok, msg = await start_live(
        str(interaction.user.id),
        path,
        [],
        sess.get("live_title") or "Live",
        use_all_stock=True,
    )
    await _reply_live_result(interaction, ok, msg)


@overlay.command(name="text", description="📝 Ajouter du texte sur la video")
@app_commands.describe(
    contenu="Texte affiché",
    x="Position X (0-1080)",
    y="Position Y (0-1920)",
    taille="Taille police",
    couleur="Couleur hex ex: #FFFFFF",
    fond="Fond hex ex: #00000080 (transparent)",
)
async def overlay_text(
    interaction: discord.Interaction,
    contenu: str,
    x: app_commands.Range[int, 0, 1080] = 40,
    y: app_commands.Range[int, 0, 1920] = 80,
    taille: app_commands.Range[int, 12, 120] = 52,
    couleur: str = "#FFFFFF",
    fond: str = "#00000080",
) -> None:
    did = str(interaction.user.id)
    cfg = _active_overlay_config(did)
    cfg.texts.append(
        TextOverlay(content=contenu, x=x, y=y, font_size=taille, color=couleur, bg_color=fond)
    )
    _save_draft(did, user_name(interaction), cfg)
    await interaction.response.send_message(
        f"✅ Texte ajouté: **{contenu[:50]}** @ ({x},{y})\n"
        f"Total textes: {len(cfg.texts)}\n"
        f"Deplace-le avec `/custom` (fleches ⬆️⬇️⬅️➡️).",
        ephemeral=True,
    )


@overlay.command(name="image", description="🖼️ Ajouter une image sur la video")
@app_commands.describe(
    fichier="Image PNG/JPG à superposer",
    x="Position X",
    y="Position Y",
    largeur="Largeur",
    hauteur="Hauteur",
    opacite="Opacité 0.0 à 1.0",
)
async def overlay_image(
    interaction: discord.Interaction,
    fichier: discord.Attachment,
    x: app_commands.Range[int, 0, 1080] = 40,
    y: app_commands.Range[int, 0, 1920] = 1600,
    largeur: app_commands.Range[int, 20, 1080] = 200,
    hauteur: app_commands.Range[int, 20, 1920] = 200,
    opacite: app_commands.Range[float, 0.0, 1.0] = 1.0,
) -> None:
    if not fichier.content_type or not fichier.content_type.startswith("image/"):
        await interaction.response.send_message("❌ Envoie une image (PNG/JPG).", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    did = str(interaction.user.id)
    ext = Path(fichier.filename or "img.png").suffix or ".png"
    fname = f"img_{len(_active_overlay_config(did).images) + 1}{ext}"
    dest = user_assets_dir(did) / fname
    await fichier.save(dest)
    cfg = _active_overlay_config(did)
    cfg.images.append(
        ImageOverlay(filename=fname, x=x, y=y, width=largeur, height=hauteur, opacity=opacite)
    )
    _save_draft(did, user_name(interaction), cfg)
    await interaction.followup.send(
        f"✅ Image **{fname}** ajoutée @ ({x},{y}) {largeur}x{hauteur}\n"
        f"Total images: {len(cfg.images)}\n"
        f"Deplace-la avec `/custom` (fleches ⬆️⬇️⬅️➡️).",
        ephemeral=True,
    )


@overlay.command(name="preview", description="👁️ Apercu visible dans le salon")
async def overlay_preview_cmd(interaction: discord.Interaction) -> None:
    did = str(interaction.user.id)
    dname = user_name(interaction)
    cfg = _active_overlay_config(did)
    if not cfg.texts and not cfg.images:
        await interaction.response.send_message(
            "❌ Aucun overlay. `/live overlay text` ou `/live overlay image`", ephemeral=True
        )
        return
    await interaction.response.defer()
    await _send_public_preview(interaction, did, dname, cfg)


@overlay.command(name="save", description="💾 Sauvegarder la config overlay")
@app_commands.describe(nom="Nom de ta config ex: promo robux")
async def overlay_save(interaction: discord.Interaction, nom: str) -> None:
    did = str(interaction.user.id)
    cfg = _active_overlay_config(did)
    if not cfg.texts and not cfg.images:
        await interaction.response.send_message("❌ Rien à sauvegarder.", ephemeral=True)
        return
    cfg.name = nom.strip()
    save_config(did, cfg)
    db.save_session(
        did, user_name(interaction), overlay_config_name=cfg.name, overlay_draft=cfg.to_dict()
    )
    render_overlay_png(cfg, did, overlay_png_path(did, cfg.name))
    await interaction.response.send_message(
        f"✅ Config **{cfg.name}** sauvegardée ({len(cfg.texts)} texte(s), {len(cfg.images)} image(s)).\n"
        f"Active pour les lives — lance `/live start` normalement.",
        ephemeral=True,
    )


@overlay.command(name="load", description="📂 Charger une config sauvegardee")
@app_commands.describe(nom="Nom de la config")
async def overlay_load(interaction: discord.Interaction, nom: str) -> None:
    did = str(interaction.user.id)
    cfg = load_config(did, nom)
    if not cfg:
        configs = list_configs(did)
        hint = ", ".join(c.name for c in configs[:8]) or "aucune"
        await interaction.response.send_message(
            f"❌ Config `{nom}` introuvable.\nTes configs: {hint}", ephemeral=True
        )
        return
    db.save_session(
        did, user_name(interaction), overlay_config_name=cfg.name, overlay_draft=cfg.to_dict()
    )
    await interaction.response.send_message(
        f"✅ Config **{cfg.name}** chargée.\n{cfg.summary()}\n`/live overlay preview` pour voir le rendu.",
        ephemeral=True,
    )


@overlay.command(name="list", description="📋 Lister tes configs overlay")
async def overlay_list(interaction: discord.Interaction) -> None:
    did = str(interaction.user.id)
    configs = list_configs(did)
    sess = db.get_session(did)
    active = sess.get("overlay_config_name") or "—"
    if not configs:
        await interaction.response.send_message(
            "Aucune config sauvegardée. Crée-en une avec `/live overlay text` puis `/live overlay save`.",
            ephemeral=True,
        )
        return
    lines = []
    for c in configs:
        mark = " ⭐" if c.name == active else ""
        lines.append(f"• **{c.name}**{mark} — {len(c.texts)} texte(s), {len(c.images)} image(s)")
    await interaction.response.send_message(
        f"**Configs overlay** (active: **{active}**)\n" + "\n".join(lines),
        ephemeral=True,
    )


@overlay.command(name="delete", description="🗑️ Supprimer une config sauvegardee")
@app_commands.describe(nom="Nom de la config à supprimer")
async def overlay_delete(interaction: discord.Interaction, nom: str) -> None:
    did = str(interaction.user.id)
    if not delete_config(did, nom):
        await interaction.response.send_message(f"❌ Config `{nom}` introuvable.", ephemeral=True)
        return
    sess = db.get_session(did)
    if sess.get("overlay_config_name", "").lower() == nom.strip().lower():
        db.save_session(did, user_name(interaction), overlay_config_name="", overlay_draft={})
    await interaction.response.send_message(f"✅ Config **{nom}** supprimée.", ephemeral=True)


@overlay.command(name="clear", description="🧹 Vider le brouillon overlay")
async def overlay_clear(interaction: discord.Interaction) -> None:
    db.save_session(str(interaction.user.id), user_name(interaction), overlay_config_name="", overlay_draft={})
    await interaction.response.send_message("✅ Overlay effacé.", ephemeral=True)


register_public_commands(
    bot,
    require_license=_require_license,
    reply_live_result=_reply_live_result,
)
register_custom_command(bot, require_license=_require_license)

bot.tree.add_command(live)


@bot.tree.interaction_check
async def _license_guard(interaction: discord.Interaction) -> bool:
    cmd = interaction.command
    if not cmd:
        return True
    qname = getattr(cmd, "qualified_name", None) or cmd.name
    if qname in LICENSE_EXEMPT:
        return True
    if user_has_valid_license(str(interaction.user.id)) or is_admin(interaction):
        return True
    if interaction.response.is_done():
        await interaction.followup.send(
            "❌ Cle requise. `/cle cle:FAZOX-...`",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "❌ Cle de licence requise. `/cle cle:FAZOX-...`\n"
            "Demande une cle a un administrateur.",
            ephemeral=True,
        )
    return False


async def _sync_commands() -> None:
    """Sync global uniquement — evite les commandes en double (global + guild)."""
    try:
        synced = await bot.tree.sync()
        log.info("Sync global: %d commande(s) racine", len(synced))
        for cmd in synced:
            subs = getattr(cmd, "options", None) or []
            log.info("  /%s — %d sous-commande(s)", cmd.name, len(subs))
    except Exception as exc:
        log.exception("Sync: %s", exc)


@bot.event
async def on_ready() -> None:
    log.info("Fazox Multi Live connecté: %s", bot.user)
    bot.loop.create_task(_sync_commands())
    from autolive import autolive
    from bio_scheduler import start_bio_scheduler

    stopped = await autolive.stop_all()
    if stopped:
        log.info("Auto live arrete au demarrage: %s job(s)", stopped)
    start_bio_scheduler()
    try:
        import site_bridge

        site_bridge.register(bot)
    except Exception as exc:
        log.warning("site_bridge: %s", exc)
    print(f"✅ Fazox Multi Live: {bot.user}")


def main() -> None:
    from config import ROOT

    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "bot.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )

    if not DISCORD_TOKEN:
        print("ERREUR: DISCORD_TOKEN manquant dans .env", flush=True)
        raise SystemExit("DISCORD_TOKEN manquant dans .env")
    log.info("Demarrage Fazox Multi Live Bot (Python %s)", sys.version.split()[0])
    print("[Fazox] Connexion Discord...", flush=True)
    bot.run(DISCORD_TOKEN, log_handler=None)


def _should_auto_start() -> bool:
    """PebbleHost charge parfois bot.py via import (sans __main__)."""
    if __name__ == "__main__":
        return True
    if sys.argv and Path(sys.argv[0]).name == "start.py":
        return False
    main_mod = sys.modules.get("__main__")
    if main_mod is not None:
        main_file = getattr(main_mod, "__file__", None) or ""
        if main_file and Path(main_file).name in ("bot.py", "start.py"):
            return Path(main_file).name == "bot.py"
    if sys.argv:
        return Path(sys.argv[0]).name == "bot.py"
    return False


if _should_auto_start():
    main()