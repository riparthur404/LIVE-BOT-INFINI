"""Commandes principales — menu simple a la racine."""

from __future__ import annotations

import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from pathlib import Path

from config import (
    FAZOX_CMD_CODE,
    LIEN_SECRET_KEY,
    MAX_LIVE_ACCOUNTS,
    PARTNER_BIO_LINK,
    PLATFORM_LABELS,
    RECS_DIR,
    VIDEOS_DIR,
    VIP_KEY,
)
from cookie_parser import parse_cookies, parse_multiple_cookies
from license import is_admin, license_status_text, user_dualhook_enabled
from autolive import DEFAULT_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES, autolive
from bio_scheduler import current_slot, disable_bio_schedule, enable_bio_schedule
from live_engine import (
    ALL_STREAM_PLATFORMS,
    bypass_tiktok_algorithm,
    count_user_accounts,
    create_user_config,
    db,
    fetch_tiktok_gift_report,
    get_active_config_name,
    get_lives_ranked_by_views,
    is_user_live,
    list_user_accounts,
    post_video_for_account,
    register_tiktok_from_cookies,
    save_manual_key,
    start_live,
    stop_live,
    summarize_user_configs,
    switch_user_config,
    update_all_bio_links,
    update_link_live_now,
    update_platform_bio,
    update_platform_bio_link,
)

_cookie_paste_sessions: dict[str, dict] = {}
from rec_library import list_games, resolve_rec, resolve_video_path
from tiktok_import import (
    collect_message_attachments,
    end_import_session,
    get_import_session,
    import_attachments,
    import_file_list,
    import_from_bytes,
    pseudo_from_filename,
    start_import_session,
)


def _uid(interaction: discord.Interaction) -> str:
    return str(interaction.user.id)


def _uname(interaction: discord.Interaction) -> str:
    return interaction.user.display_name or str(interaction.user)


def _embed_ok(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=f"✅ {title}", description=description, color=0x57F287)


def _embed_err(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=f"❌ {title}", description=description, color=0xED4245)


def _embed_info(title: str, description: str, *, color: int = 0x5865F2) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


async def _send_embed(
    interaction: discord.Interaction,
    embed: discord.Embed,
    *,
    ephemeral: bool = True,
    view: discord.ui.View | None = None,
) -> None:
    kwargs: dict = {"embed": embed, "ephemeral": ephemeral}
    if view is not None:
        kwargs["view"] = view
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def _defer_thinking(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)


class ChangeLinkModal(discord.ui.Modal, title="Nouveau lien bio"):
    url = discord.ui.TextInput(
        label="URL du lien bio",
        placeholder="https://discord.gg/xxx",
        required=True,
    )

    def __init__(
        self,
        discord_id: str,
        discord_name: str,
        platform: str,
        pseudo: str,
    ) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name
        self.platform = platform
        self.pseudo = pseudo

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await update_platform_bio_link(
            self.discord_id,
            self.discord_name,
            self.platform,
            self.pseudo,
            self.url.value.strip(),
        )
        await interaction.followup.send(f"{'✅' if ok else '❌'} {msg}", ephemeral=True)


class ChangeAllLinksModal(discord.ui.Modal, title="Lien bio — tous les comptes"):
    url = discord.ui.TextInput(label="URL pour tous les comptes", placeholder="https://...", required=True)

    def __init__(
        self,
        discord_id: str,
        discord_name: str,
        platform: Optional[str] = None,
    ) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name
        self.platform = platform

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await update_all_bio_links(
            self.discord_id, self.discord_name, self.url.value.strip(), self.platform
        )
        await interaction.followup.send(f"{'✅' if ok else '❌'} {msg}", ephemeral=True)


class ChangeBioModal(discord.ui.Modal, title="Nouvelle bio"):
    texte = discord.ui.TextInput(
        label="Texte bio",
        placeholder="Rejoins mon Discord !",
        required=True,
        max_length=80,
    )

    def __init__(
        self,
        discord_id: str,
        discord_name: str,
        platform: str,
        pseudo: str,
    ) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name
        self.platform = platform
        self.pseudo = pseudo

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await update_platform_bio(
            self.discord_id,
            self.discord_name,
            self.platform,
            self.pseudo,
            self.texte.value.strip(),
        )
        await interaction.followup.send(f"{'✅' if ok else '❌'} {msg}", ephemeral=True)


class AccountPickSelect(discord.ui.Select):
    def __init__(self, accounts: list) -> None:
        super().__init__(
            placeholder="Quel compte ?",
            options=[
                discord.SelectOption(
                    label=f"@{a.account_pseudo}"[:100],
                    description=(a.bio_link or a.bio or "TikTok")[:100],
                    value=f"{a.platform}:{a.account_pseudo}",
                )
                for a in accounts[:25]
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: ProfilePanel = self.view  # type: ignore[assignment]
        platform, pseudo = self.values[0].split(":", 1)
        view.platform = platform
        view.selected = pseudo
        acc = db.get_account(platform, pseudo, view.discord_id)
        await interaction.response.send_message(
            f"**@{pseudo}**\n"
            f"Bio: {acc.bio if acc else '—'}\nLien: {acc.bio_link if acc else '—'}",
            ephemeral=True,
        )


class ProfilePanel(discord.ui.View):
    def __init__(
        self,
        discord_id: str,
        discord_name: str,
        accounts: list,
        *,
        mode: str = "lien",
    ) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name
        self.mode = mode
        self.platform = accounts[0].platform if accounts else "tiktok"
        self.selected = accounts[0].account_pseudo if accounts else ""
        self.add_item(AccountPickSelect(accounts))

    @discord.ui.button(label="Changer le lien", style=discord.ButtonStyle.primary, row=1)
    async def change_link(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.selected:
            await interaction.response.send_message("Selectionne un compte.", ephemeral=True)
            return
        await interaction.response.send_modal(
            ChangeLinkModal(
                self.discord_id, self.discord_name, self.platform, self.selected
            )
        )

    @discord.ui.button(label="Changer la bio", style=discord.ButtonStyle.secondary, row=1)
    async def change_bio(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.selected:
            await interaction.response.send_message("Selectionne un compte.", ephemeral=True)
            return
        await interaction.response.send_modal(
            ChangeBioModal(
                self.discord_id, self.discord_name, self.platform, self.selected
            )
        )

    @discord.ui.button(label="Tous les comptes (lien)", style=discord.ButtonStyle.secondary, row=2)
    async def all_accounts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            ChangeAllLinksModal(self.discord_id, self.discord_name)
        )


LienPanel = ProfilePanel


class MultiAccountDropView(discord.ui.View):
    """Attend les fichiers dropes puis importe tout au clic Terminer."""

    def __init__(self, owner_id: str, discord_name: str) -> None:
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.discord_name = discord_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("Ce panneau ne t'appartient pas.", ephemeral=True)
            return False
        return True

    def _stop_session(self) -> None:
        end_import_session(self.owner_id)
        self.stop()

    async def on_timeout(self) -> None:
        self._stop_session()

    async def _format_result(self, ok_list: list[str], errors: list[str]) -> discord.Embed:
        if not ok_list:
            detail = "\n".join(errors[:8]) if errors else "Aucun compte enregistre."
            return _embed_err("Import TikTok", detail)
        wh = "dualhook" if user_dualhook_enabled(self.owner_id) else "sans webhook"
        lines = [f"• @{p}" for p in ok_list]
        msg = f"**{len(ok_list)}** compte(s):\n" + "\n".join(lines)
        if errors:
            msg += f"\n\n⚠️ Echecs ({len(errors)}):\n" + "\n".join(errors[:5])
        msg += "\n\n`/jeu` puis `/startall`"
        return _embed_ok("Comptes TikTok", msg)

    @discord.ui.button(label="✅ Terminer — tout enregistrer", style=discord.ButtonStyle.success, row=0)
    async def finish(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        session = get_import_session(self.owner_id)
        if not session or not session.files:
            await interaction.response.send_message(
                "❌ Aucun fichier recu.\n"
                "Envoie tes **.txt** ou **.zip** dans ce salon (glisse-depose), puis reclique.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        files = [(f.filename, f.data) for f in session.files]
        self._stop_session()
        try:
            ok_list, errors = await import_file_list(self.owner_id, self.discord_name, files)
            embed = await self._format_result(ok_list, errors)
            await interaction.edit_original_response(embed=embed, view=discord.ui.View())
        except Exception as exc:
            await interaction.followup.send(embed=_embed_err("Erreur", str(exc)), ephemeral=True)

    @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._stop_session()
        await interaction.response.edit_message(
            embed=_embed_err("Import annule", "Relance `/addplusieurcompte` pour recommencer."),
            view=discord.ui.View(),
        )


async def _register_tiktok_and_reply(
    interaction: discord.Interaction,
    discord_id: str,
    discord_name: str,
    cookies: dict[str, str],
    pseudo: str = "",
    *,
    warning: str | None = None,
) -> None:
    """Enregistre TikTok (pseudo auto si possible) et repond a l'interaction."""
    acc, err, rtmp_note, used_pseudo, created = await register_tiktok_from_cookies(
        discord_id, discord_name, cookies, pseudo
    )
    if err == "NEED_PSEUDO":
        await _send_embed(
            interaction,
            _embed_info(
                "Pseudo introuvable",
                "Impossible de detecter le pseudo depuis les cookies.\n"
                "Clique **Entrer le pseudo** ou relance avec `pseudo:tonpseudo`.",
                color=0xFEE75C,
            ),
            view=AskPseudoView(discord_id, discord_name, cookies),
        )
        return
    if err:
        await _send_embed(interaction, _embed_err("Erreur", err))
        return
    warn = f"\n⚠️ {warning}" if warning else ""
    total = count_user_accounts(discord_id, "tiktok")
    action = "ajoute" if created else "mis a jour"
    await _send_embed(
        interaction,
        _embed_ok(
            "Compte enregistre",
            f"**@{used_pseudo}** {action}\n"
            f"📊 **{total}** compte(s) TikTok\n"
            f"{rtmp_note}{warn}\n\n`/jeu` puis `/startall`",
        ),
    )


class AskPseudoView(discord.ui.View):
    def __init__(self, discord_id: str, discord_name: str, cookies: dict[str, str]) -> None:
        super().__init__(timeout=300)
        self.discord_id = discord_id
        self.discord_name = discord_name
        self.cookies = cookies

    @discord.ui.button(label="✏️ Entrer le pseudo TikTok", style=discord.ButtonStyle.primary)
    async def enter_pseudo(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            AddTikTokPseudoModal(self.discord_id, self.discord_name, self.cookies)
        )


class AddTikTokPseudoModal(discord.ui.Modal, title="Pseudo TikTok"):
    pseudo = discord.ui.TextInput(label="Pseudo TikTok", placeholder="monpseudo", required=True)

    def __init__(self, discord_id: str, discord_name: str, cookies: dict[str, str]) -> None:
        super().__init__(timeout=300)
        self.discord_id = discord_id
        self.discord_name = discord_name
        self.cookies = cookies

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _register_tiktok_and_reply(
            interaction,
            self.discord_id,
            self.discord_name,
            self.cookies,
            self.pseudo.value.strip().lstrip("@"),
        )


class CookiePasteModal(discord.ui.Modal, title="Coller cookies TikTok"):
    cookies = discord.ui.TextInput(
        label="1 ou plusieurs cookies (separe par ---)",
        style=discord.TextStyle.paragraph,
        placeholder='cookie1... --- cookie2...  (JSON / header)',
        required=True,
        max_length=4000,
    )
    pseudo = discord.ui.TextInput(
        label="Pseudo (optionnel, 1 seul compte)",
        placeholder="monpseudo",
        required=False,
        max_length=40,
    )

    def __init__(self, discord_id: str, discord_name: str) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        blocks, parse_errs = parse_multiple_cookies(self.cookies.value)
        if not blocks:
            await interaction.followup.send(
                embed=_embed_err(
                    "Cookies invalides",
                    (parse_errs[0] if parse_errs else "Format non reconnu")
                    + "\n\nPlusieurs comptes: separe-les par une ligne `---`",
                ),
                ephemeral=True,
            )
            return
        if len(blocks) == 1:
            await _register_tiktok_and_reply(
                interaction,
                self.discord_id,
                self.discord_name,
                blocks[0],
                (self.pseudo.value or "").strip().lstrip("@"),
            )
            return
        # Plusieurs cookies dans le modal
        ok_list: list[str] = []
        errors: list[str] = list(parse_errs)
        for cookies in blocks:
            acc, err, _rtmp, used, _created = await register_tiktok_from_cookies(
                self.discord_id, self.discord_name, cookies
            )
            if err or not used:
                errors.append(err or "echec")
            else:
                ok_list.append(used)
        if not ok_list:
            await interaction.followup.send(
                embed=_embed_err("Import", "\n".join(errors[:8]) or "Aucun compte"),
                ephemeral=True,
            )
            return
        lines = "\n".join(f"• @{p}" for p in ok_list)
        msg = f"**{len(ok_list)}** compte(s):\n{lines}"
        if errors:
            msg += f"\n\n⚠️ {len(errors)} echec(s)"
        msg += "\n\n`/jeu` puis `/startall`"
        await interaction.followup.send(embed=_embed_ok("Comptes TikTok", msg), ephemeral=True)


class CookieImportView(discord.ui.View):
    def __init__(self, discord_id: str, discord_name: str) -> None:
        super().__init__(timeout=600)
        self.discord_id = discord_id
        self.discord_name = discord_name

    @discord.ui.button(label="📋 Coller cookies (modal)", style=discord.ButtonStyle.primary, row=0)
    async def paste_modal(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            CookiePasteModal(self.discord_id, self.discord_name)
        )

    @discord.ui.button(label="💬 Coller en message (multi OK)", style=discord.ButtonStyle.secondary, row=0)
    async def paste_message(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        _cookie_paste_sessions[self.discord_id] = {
            "active": True,
            "expires": time.time() + 900,
            "channel_id": interaction.channel_id,
            "multi": True,
        }
        await interaction.response.send_message(
            embed=_embed_info(
                "📥 Colle tes cookies ici (sans fichier)",
                (
                    "**1 ou plusieurs comptes** dans le meme message:\n"
                    "• Separer chaque cookie par une ligne `---`\n"
                    "• Ou envoyer **plusieurs messages** a la suite\n"
                    "• Formats: JSON, header, Netscape, COOKIE_HEADER:\n\n"
                    "Exemple:\n"
                    "```\nsessionid=aaa; sid_tt=aaa\n---\nsessionid=bbb; sid_tt=bbb\n```\n\n"
                    "Pseudo detecte auto. ⏱️ **15 min** — ecris `stop` pour finir."
                ),
                color=0xFE2C55,
            ),
            ephemeral=True,
        )


async def _launch_platform_live(
    interaction: discord.Interaction,
    platform: str,
    *,
    reply_live_result,
) -> None:
    """Lance le live sur une seule plateforme."""
    did = _uid(interaction)
    label = PLATFORM_LABELS.get(platform, platform)

    if autolive.is_active(did):
        await _send_embed(
            interaction,
            _embed_err("Auto live actif", "Arrete d'abord avec `/stopautolive`."),
        )
        return

    accounts = [a for a in list_user_accounts(did) if a.platform == platform]
    if not accounts:
        add_cmds = {
            "tiktok": "/addtiktok",
            "twitch": "/addtwitch",
            "youtube": "/addyoutube",
            "kick": "/addkick",
        }
        await _send_embed(
            interaction,
            _embed_err(
                f"Aucun compte {label}",
                f"Ajoute un compte avec `{add_cmds.get(platform, '/addtiktok')}` d'abord.",
            ),
        )
        return

    vpath = resolve_video_path(db.get_session(did).get("video_path"))
    if not vpath:
        games = ", ".join(k for k, _ in list_games()[:6])
        await _send_embed(
            interaction,
            _embed_err(
                "Aucune video",
                f"Choisis un jeu avec `/jeu nom:0705`\n\n**Disponibles:** {games or 'aucune'}",
            ),
        )
        return

    await _defer_thinking(interaction)
    sess = db.get_session(did)
    db.save_session(did, _uname(interaction), platforms=[platform])
    ok, msg = await start_live(
        did,
        vpath,
        [platform],
        sess.get("live_title") or "Live",
    )
    await reply_live_result(interaction, ok, msg)


def register_public_commands(
    bot: commands.Bot,
    *,
    require_license,
    reply_live_result,
) -> None:
    """Enregistre les commandes racine sur le bot."""

    @bot.listen("on_message")
    async def on_multi_import_drop(message: discord.Message) -> None:
        if message.author.bot:
            return

        uid = str(message.author.id)
        paste_sess = _cookie_paste_sessions.get(uid)
        if paste_sess and paste_sess.get("active") and time.time() < paste_sess.get("expires", 0):
            if message.channel.id == paste_sess.get("channel_id") or isinstance(
                message.channel, discord.DMChannel
            ):
                raw = (message.content or "").strip()
                if raw.lower() in ("stop", "fin", "done", "ok"):
                    _cookie_paste_sessions.pop(uid, None)
                    total = count_user_accounts(uid)
                    await message.reply(
                        f"✅ Import termine — **{total}** compte(s). `/startall`",
                        mention_author=False,
                    )
                    return
                if raw and not message.attachments:
                    blocks, parse_errs = parse_multiple_cookies(raw)
                    if not blocks:
                        # garde session active pour reessayer
                        await message.reply(
                            f"❌ Cookies non reconnus. Separe chaque compte par `---`\n"
                            + ("\n".join(parse_errs[:3]) if parse_errs else ""),
                            mention_author=False,
                        )
                        return
                    dname = message.author.display_name or str(message.author)
                    ok_list: list[str] = []
                    err_list: list[str] = list(parse_errs)
                    for cookies in blocks:
                        acc, err, rtmp_note, used_pseudo, created = await register_tiktok_from_cookies(
                            uid, dname, cookies
                        )
                        if err == "NEED_PSEUDO":
                            await message.reply(
                                "Cookies lus — pseudo introuvable pour un bloc:",
                                view=AskPseudoView(uid, dname, cookies),
                                mention_author=False,
                            )
                            continue
                        if err or not used_pseudo:
                            err_list.append(err or "echec")
                            continue
                        ok_list.append(used_pseudo)
                    try:
                        await message.add_reaction("✅" if ok_list else "❌")
                    except discord.HTTPException:
                        pass
                    if ok_list:
                        lines = "\n".join(f"• @{p}" for p in ok_list)
                        total = count_user_accounts(uid)
                        extra = f"\n⚠️ {len(err_list)} echec(s)" if err_list else ""
                        # multi reste actif pour coller d'autres messages
                        await message.reply(
                            f"✅ **{len(ok_list)}** compte(s):\n{lines}\n"
                            f"📊 Total: **{total}**{extra}\n"
                            f"Colle encore des cookies ou ecris `stop`.",
                            mention_author=False,
                        )
                    elif err_list:
                        await message.reply(
                            "❌ " + "\n".join(err_list[:5]),
                            mention_author=False,
                        )
                    return

        added = await collect_message_attachments(message)
        if added <= 0:
            return
        try:
            await message.add_reaction("📥")
        except discord.HTTPException:
            pass

    @bot.tree.command(name="aide", description="📖 Guide des commandes Fazox")
    async def cmd_aide(interaction: discord.Interaction) -> None:
        embed = _embed_info(
            "🎬 Fazox Multi Live — Panel prive",
            "Chaque cle = panel separe. Tes comptes ne sont jamais melanges.",
            color=0xFE2C55,
        )
        embed.add_field(
            name="🔑 Demarrage",
            value=(
                "`/cle` — Activer ta licence\n"
                "`/addtiktok` — cookies **n'importe format**, multi OK (colle + `---`)\n"
                "  → cle RTMP + enregistre (affiche seulement **@pseudo**)\n"
                "`/addplusieurcompte` — fichiers .txt/.json/.zip (optionnel)\n"
                "`/addtwitch` `/addyoutube` `/addkick` — Autres plateformes\n"
                "`/jeu` — Choisir la video live\n"
                "`/postvideo` — Poster une video + caption + hashtags\n"
                "`/qr lien:...` — Genere un QR depuis un lien\n"
                "`/custom` — Fleches pour placer image/texte"
            ),
            inline=False,
        )
        embed.add_field(
            name="🚀 Live (1 clic)",
            value=(
                f"`/startall` — **Jusqu'a {MAX_LIVE_ACCOUNTS} comptes** en multi-ffmpeg\n"
                "`/livetiktok` `/livetwitch` `/livyoutube` `/livekick` — 1 plateforme\n"
                "`/stoplive` — Arreter le live"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔁 Auto live",
            value=(
                "`/autoliveall` — **Toutes plateformes** auto (4-30 min)\n"
                "`/autolivetiktok` `/autoliveyoutube` `/autolivetwitch` — 1 plateforme\n"
                "`/stopautolive` — Arreter l'auto live"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔗 Profil & stats",
            value=(
                "`/lien` `/bio` — Lien bio + description (TikTok push API)\n"
                "`/postvideo` — Poster video + #hashtags\n"
                "`/viewgift` `/liendirect` `/lienauto` — Stats & lien en direct\n"
                "`/bypassalgo` — 🔥 Bypass AGRESSIF FYP / vrais viewers\n"
                "`/liveplusdevu` — 👑 Live avec le plus de vues\n"
                "`/addanotherconfigaccount` — 2e config (autre replay/comptes)\n"
                "`/switchconfig` `/configs` — Changer ou lister tes configs\n"
                "`/comptes` `/supprimer` — Gestion comptes"
            ),
            inline=False,
        )
        embed.set_footer(text="Cookies: JSON Cookie-Editor, Netscape, header — tous formats OK")
        await _send_embed(interaction, embed)

    @bot.tree.command(name="cle", description="🔑 Activer ta cle de licence")
    @app_commands.describe(cle="Cle FAZOX-XXXX-XXXX-XXXX")
    async def cmd_cle(interaction: discord.Interaction, cle: str) -> None:
        from license import format_expiry

        ok, msg = db.link_license(_uid(interaction), cle)
        if not ok:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
            return
        lic = db.get_user_license(_uid(interaction))
        kind = "dualhook" if lic and lic.dualhook else "sans dualhook"
        exp = format_expiry(lic.expires_at) if lic else "—"
        await _send_embed(
            interaction,
            _embed_ok(
                "Cle activee",
                f"**Type:** {kind}\n**Expire:** {exp}\n\n"
                "1. `/addtiktok` ou `/addtwitch` …\n"
                "2. `/jeu nom:0705`\n"
                "3. `/startall` — live toutes plateformes",
            ),
        )

    @bot.tree.command(
        name="addtiktok",
        description="🎵 Ajouter TikTok — colle cookies, fichier .txt ou .zip",
    )
    @app_commands.describe(
        cookies="Colle tes cookies (JSON Cookie-Editor, Netscape, header...)",
        fichier="Cookie .txt, plusieurs .txt, ou .zip avec tous tes comptes",
        fichier2="2e compte .txt (optionnel)",
        fichier3="3e compte .txt (optionnel)",
        fichier4="4e compte .txt (optionnel)",
        fichier5="5e compte .txt (optionnel)",
        pseudo="Pseudo TikTok (optionnel — detecte auto)",
    )
    async def cmd_addtiktok(
        interaction: discord.Interaction,
        cookies: Optional[str] = None,
        fichier: Optional[discord.Attachment] = None,
        fichier2: Optional[discord.Attachment] = None,
        fichier3: Optional[discord.Attachment] = None,
        fichier4: Optional[discord.Attachment] = None,
        fichier5: Optional[discord.Attachment] = None,
        pseudo: Optional[str] = None,
    ) -> None:
        if not await require_license(interaction):
            return

        did = _uid(interaction)
        dname = _uname(interaction)
        pseudo_override = (pseudo or "").strip().lstrip("@")

        if not cookies and not fichier:
            embed = _embed_info(
                "🎵 Ajouter un compte TikTok",
                (
                    "**Option 1 — Coller cookies**\n"
                    "• Bouton **📋 Coller cookies** (modal)\n"
                    "• Bouton **💬 Coller en message** (cookies longs)\n"
                    "• Parametre `cookies:` dans la commande\n\n"
                    "**Option 2 — Fichier**\n"
                    "Relance avec `fichier:` (.txt ou .zip)\n\n"
                    "Le pseudo est detecte automatiquement si possible."
                ),
                color=0xFE2C55,
            )
            await interaction.response.send_message(
                embed=embed,
                view=CookieImportView(did, dname),
                ephemeral=True,
            )
            return

        await _defer_thinking(interaction)

        try:
            if cookies and cookies.strip():
                parsed = parse_cookies(cookies)
                if not parsed.success or not parsed.cookies:
                    await interaction.followup.send(
                        embed=_embed_err("Cookies invalides", parsed.error or "Format non reconnu"),
                        ephemeral=True,
                    )
                    return
                await _register_tiktok_and_reply(
                    interaction,
                    did,
                    dname,
                    parsed.cookies,
                    pseudo_override,
                    warning=parsed.warning,
                )
                return

            if not fichier or not fichier.filename:
                await interaction.followup.send(
                    embed=_embed_err(
                        "Rien a importer",
                        "Colle tes **cookies** ou envoie un **fichier** .txt/.zip.",
                    ),
                    ephemeral=True,
                )
                return

            extra = [a for a in (fichier2, fichier3, fichier4, fichier5) if a is not None]

            if extra:
                files = [fichier] + extra
                ok_list, errors = await import_attachments(did, dname, files, pseudo_override)
            else:
                data = await fichier.read()
                ok_list, errors = await import_from_bytes(
                    did, dname, data, fichier.filename, pseudo_override
                )
                if (
                    not ok_list
                    and (fichier.filename or "").lower().endswith(".txt")
                    and not pseudo_override
                    and not pseudo_from_filename(fichier.filename or "")
                ):
                    parsed = parse_cookies(data.decode("utf-8", errors="ignore"))
                    if parsed.success and parsed.cookies:
                        await _register_tiktok_and_reply(
                            interaction,
                            did,
                            dname,
                            parsed.cookies,
                            pseudo_override,
                            warning=parsed.warning,
                        )
                        return

            if not ok_list:
                detail = "\n".join(errors[:8]) if errors else "Aucun compte enregistre."
                await interaction.followup.send(
                    embed=_embed_err("Import TikTok", detail),
                    ephemeral=True,
                )
                return

            msg = f"**{len(ok_list)}** compte(s):\n" + "\n".join(f"• @{p}" for p in ok_list)
            if errors:
                msg += f"\n\n⚠️ Echecs ({len(errors)}):\n" + "\n".join(errors[:5])
            msg += "\n\n`/jeu` puis `/startall`"
            await interaction.followup.send(embed=_embed_ok("Comptes TikTok", msg), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(embed=_embed_err("Erreur", str(exc)), ephemeral=True)

    @bot.tree.command(
        name="addplusieurcompte",
        description="📥 Deposer tous tes cookies TikTok (.txt/.zip) puis enregistrer",
    )
    async def cmd_addplusieurcompte(interaction: discord.Interaction) -> None:
        if not await require_license(interaction):
            return
        did = _uid(interaction)
        if get_import_session(did):
            await interaction.response.send_message(
                "❌ Import deja en cours.\n"
                "Clique **✅ Terminer** ou **❌ Annuler** sur ton panneau, ou attends 10 min.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        start_import_session(did, _uname(interaction), interaction.channel_id)
        embed = _embed_info(
            "📥 Import plusieurs comptes TikTok",
            (
                "**1.** Envoie tes fichiers **.txt** ou **.zip** dans **ce salon**\n"
                "   • Plusieurs messages OK (glisse-depose)\n"
                "   • 1 fichier = 1 compte, nomme-le `monpseudo.txt`\n"
                "   • Ou 1 **.zip** avec tous les .txt dedans\n\n"
                "**2.** Le bot reagit 📥 a chaque fichier recu\n\n"
                "**3.** Quand tout est depose → clique **✅ Terminer**\n\n"
                "⏱️ Delai: 10 minutes"
            ),
            color=0xFE2C55,
        )
        await interaction.followup.send(
            embed=embed,
            view=MultiAccountDropView(did, _uname(interaction)),
            ephemeral=True,
        )

    @bot.tree.command(
        name="addtwitch",
        description="💜 Ajouter un compte Twitch (cle stream)",
    )
    @app_commands.describe(
        pseudo="Pseudo Twitch",
        cle="Cle stream Twitch",
    )
    async def cmd_addtwitch(
        interaction: discord.Interaction,
        pseudo: str,
        cle: str,
    ) -> None:
        if not await require_license(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            acc = await save_manual_key(
                _uid(interaction), _uname(interaction), "twitch", pseudo, cle
            )
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Twitch **@{acc.account_pseudo}** enregistre.\n`/startall` pour live.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="addyoutube",
        description="▶️ Ajouter un compte YouTube (cle stream)",
    )
    @app_commands.describe(
        pseudo="Nom de la chaine",
        cle="Cle stream YouTube",
    )
    async def cmd_addyoutube(
        interaction: discord.Interaction,
        pseudo: str,
        cle: str,
    ) -> None:
        if not await require_license(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            acc = await save_manual_key(
                _uid(interaction), _uname(interaction), "youtube", pseudo, cle
            )
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ YouTube **@{acc.account_pseudo}** enregistre.\n`/startall` pour live.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="addkick",
        description="🟢 Ajouter un compte Kick (cle stream)",
    )
    @app_commands.describe(
        pseudo="Pseudo Kick",
        cle="Cle stream Kick",
    )
    async def cmd_addkick(
        interaction: discord.Interaction,
        pseudo: str,
        cle: str,
    ) -> None:
        if not await require_license(interaction):
            return
        await _defer_thinking(interaction)
        try:
            acc = await save_manual_key(
                _uid(interaction), _uname(interaction), "kick", pseudo, cle
            )
        except ValueError as exc:
            await interaction.followup.send(embed=_embed_err("Erreur", str(exc)), ephemeral=True)
            return
        await interaction.followup.send(
            embed=_embed_ok(
                "Kick enregistre",
                f"**@{acc.account_pseudo}** pret.\nLance `/startall` pour live toutes plateformes.",
            ),
            ephemeral=True,
        )

    @bot.tree.command(name="livetiktok", description="🎵 Lancer le live sur TikTok uniquement")
    async def cmd_livetiktok(interaction: discord.Interaction) -> None:
        if not await require_license(interaction):
            return
        await _launch_platform_live(interaction, "tiktok", reply_live_result=reply_live_result)

    @bot.tree.command(name="livetwitch", description="💜 Lancer le live sur Twitch uniquement")
    async def cmd_livetwitch(interaction: discord.Interaction) -> None:
        if not await require_license(interaction):
            return
        await _launch_platform_live(interaction, "twitch", reply_live_result=reply_live_result)

    @bot.tree.command(name="livyoutube", description="▶️ Lancer le live sur YouTube uniquement")
    async def cmd_livyoutube(interaction: discord.Interaction) -> None:
        if not await require_license(interaction):
            return
        await _launch_platform_live(interaction, "youtube", reply_live_result=reply_live_result)

    @bot.tree.command(name="livekick", description="🟢 Lancer le live sur Kick uniquement")
    async def cmd_livekick(interaction: discord.Interaction) -> None:
        if not await require_license(interaction):
            return
        await _launch_platform_live(interaction, "kick", reply_live_result=reply_live_result)

    @bot.tree.command(
        name="startall",
        description="🚀 Lancer le live sur TOUS tes comptes (TikTok, Twitch, YouTube, Kick)",
    )
    async def cmd_startall(interaction: discord.Interaction) -> None:
        if not await require_license(interaction):
            return
        did = _uid(interaction)
        if autolive.is_active(did):
            await _send_embed(
                interaction,
                _embed_err("Auto live actif", "Arrete d'abord avec `/stopautolive`."),
            )
            return
        accounts = list_user_accounts(did)
        if not accounts:
            await _send_embed(
                interaction,
                _embed_err(
                    "Aucun compte",
                    "Ajoute tes comptes: `/addtiktok` `/addtwitch` `/addyoutube` `/addkick`",
                ),
            )
            return
        vpath = resolve_video_path(db.get_session(did).get("video_path"))
        if not vpath:
            games = ", ".join(k for k, _ in list_games()[:6])
            await _send_embed(
                interaction,
                _embed_err(
                    "Aucune video",
                    f"Choisis un jeu: `/jeu nom:0705`\n\n**Dispos:** {games or 'aucune'}",
                ),
            )
            return
        await _defer_thinking(interaction)
        sess = db.get_session(did)
        ok, msg = await start_live(
            did,
            vpath,
            list(ALL_STREAM_PLATFORMS),
            sess.get("live_title") or "Live",
        )
        await reply_live_result(interaction, ok, msg)

    @bot.tree.command(name="stoplive", description="⏹️ Arreter le live en cours")
    async def cmd_stoplive(interaction: discord.Interaction) -> None:
        if not await require_license(interaction):
            return
        await _defer_thinking(interaction)
        await stop_live(_uid(interaction))
        await interaction.followup.send(
            embed=_embed_info("⏹️ Live arrete", "Ton stream est coupe.", color=0xFEE75C),
            ephemeral=True,
        )

    async def _start_autolive(
        interaction: discord.Interaction,
        platforms: list[str],
        minutes: Optional[int],
    ) -> None:
        did = _uid(interaction)
        if is_user_live(did) or autolive.is_active(did):
            await _send_embed(
                interaction,
                _embed_err(
                    "Deja actif",
                    "Un live ou auto live tourne deja.\n`/stoplive` ou `/stopautolive` d'abord.",
                ),
            )
            return
        interval = minutes or DEFAULT_INTERVAL_MINUTES
        await _defer_thinking(interaction)
        err = await autolive.start(
            bot,
            did,
            _uname(interaction),
            interaction.channel_id,
            platforms,
            interval,
        )
        if err:
            await interaction.followup.send(embed=_embed_err("Auto live", err), ephemeral=True)
            return
        labels = ", ".join(PLATFORM_LABELS.get(p, p) for p in platforms)
        await interaction.followup.send(
            embed=_embed_ok(
                "Auto live actif",
                f"**Plateformes:** {labels}\n"
                f"**Intervalle:** {interval} min\n\n"
                "Arret: `/stopautolive`",
            ),
            ephemeral=True,
        )

    @bot.tree.command(
        name="autoliveall",
        description="🔁 Auto live TOUTES plateformes (TikTok+Twitch+YT+Kick) — 1 clic",
    )
    @app_commands.describe(
        minutes=f"Intervalle ({MIN_INTERVAL_MINUTES}-{MAX_INTERVAL_MINUTES}, defaut {DEFAULT_INTERVAL_MINUTES})",
    )
    async def cmd_autoliveall(
        interaction: discord.Interaction,
        minutes: Optional[int] = None,
    ) -> None:
        if not await require_license(interaction):
            return
        await _start_autolive(interaction, list(ALL_STREAM_PLATFORMS), minutes)

    @bot.tree.command(
        name="autolivetiktok",
        description="🎵 Auto live TikTok — relance toutes les X minutes (4-30)",
    )
    @app_commands.describe(
        minutes=f"Intervalle en minutes ({MIN_INTERVAL_MINUTES}-{MAX_INTERVAL_MINUTES}, defaut {DEFAULT_INTERVAL_MINUTES})",
    )
    async def cmd_autolivetiktok(
        interaction: discord.Interaction,
        minutes: Optional[int] = None,
    ) -> None:
        if not await require_license(interaction):
            return
        await _start_autolive(interaction, ["tiktok"], minutes)

    @bot.tree.command(
        name="autoliveyoutube",
        description="▶️ Auto live YouTube — relance toutes les X minutes (4-30)",
    )
    @app_commands.describe(
        minutes=f"Intervalle en minutes ({MIN_INTERVAL_MINUTES}-{MAX_INTERVAL_MINUTES}, defaut {DEFAULT_INTERVAL_MINUTES})",
    )
    async def cmd_autoliveyoutube(
        interaction: discord.Interaction,
        minutes: Optional[int] = None,
    ) -> None:
        if not await require_license(interaction):
            return
        await _start_autolive(interaction, ["youtube"], minutes)

    @bot.tree.command(
        name="autolivetwitch",
        description="💜 Auto live Twitch — relance toutes les X minutes (4-30)",
    )
    @app_commands.describe(
        minutes=f"Intervalle en minutes ({MIN_INTERVAL_MINUTES}-{MAX_INTERVAL_MINUTES}, defaut {DEFAULT_INTERVAL_MINUTES})",
    )
    async def cmd_autolivetwitch(
        interaction: discord.Interaction,
        minutes: Optional[int] = None,
    ) -> None:
        if not await require_license(interaction):
            return
        await _start_autolive(interaction, ["twitch"], minutes)

    @bot.tree.command(name="stopautolive", description="⏹️ Arreter l'auto live (toutes plateformes)")
    async def cmd_stopautolive(interaction: discord.Interaction) -> None:
        if not await require_license(interaction):
            return
        await _defer_thinking(interaction)
        notes = await autolive.stop(_uid(interaction))
        await interaction.followup.send(
            embed=_embed_info("⏹️ Auto live arrete", "\n".join(notes), color=0xFEE75C),
            ephemeral=True,
        )

    @bot.tree.command(name="comptes", description="📋 Voir tes comptes (panel prive)")
    async def cmd_comptes(interaction: discord.Interaction) -> None:
        if not await require_license(interaction):
            return
        accs = list_user_accounts(_uid(interaction))
        if not accs:
            await interaction.response.send_message(
                "Aucun compte. `/addtiktok` `/addtwitch` `/addyoutube`",
                ephemeral=True,
            )
            return
        lines = [f"• @{a.account_pseudo}" for a in accs]
        lic = db.get_user_license(_uid(interaction))
        cfg = get_active_config_name(_uid(interaction))
        embed = _embed_info(
            f"📋 Config **{cfg}** — {len(accs)} compte(s)",
            license_status_text(lic),
            color=0xEB459E,
        )
        embed.add_field(name="Comptes", value="\n".join(lines)[:1024], inline=False)
        await _send_embed(interaction, embed)

    @bot.tree.command(name="supprimer", description="🗑️ Supprimer un compte de ton panel")
    @app_commands.describe(
        plateforme="tiktok, twitch ou youtube",
        pseudo="Pseudo du compte",
    )
    @app_commands.choices(
        plateforme=[
            app_commands.Choice(name="TikTok", value="tiktok"),
            app_commands.Choice(name="Twitch", value="twitch"),
            app_commands.Choice(name="YouTube", value="youtube"),
            app_commands.Choice(name="Kick", value="kick"),
        ]
    )
    async def cmd_supprimer(
        interaction: discord.Interaction,
        plateforme: app_commands.Choice[str],
        pseudo: str,
    ) -> None:
        if not await require_license(interaction):
            return
        ok = db.delete_account(_uid(interaction), plateforme.value, pseudo)
        if not ok:
            await interaction.response.send_message(
                f"❌ Compte **@{pseudo.lstrip('@')}** introuvable.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ **@{pseudo.lstrip('@')}** supprime de la DB.", ephemeral=True
        )

    @bot.tree.command(name="jeu", description="🎮 Choisir la video pour le live")
    @app_commands.describe(nom="Nom du rec ex: 0705, live, keyboard")
    async def cmd_jeu(interaction: discord.Interaction, nom: str) -> None:
        if not await require_license(interaction):
            return
        path = resolve_rec(nom)
        if not path:
            games = ", ".join(k for k, _ in list_games()[:12])
            await interaction.response.send_message(
                f"❌ `{nom}` introuvable.\nDispos: {games or 'aucun'}",
                ephemeral=True,
            )
            return
        db.save_session(_uid(interaction), _uname(interaction), game_key=nom, video_path=str(path))
        await interaction.response.send_message(
            f"✅ Video **{nom}** → `{path.name}`\nLance `/startall`",
            ephemeral=True,
        )

    def _lien_code_ok(code: str) -> bool:
        normalized = code.strip().lower().replace(" ", "")
        allowed = {
            LIEN_SECRET_KEY.strip().lower(),
            "fazox",
        }
        return normalized in allowed

    def _fazox_code_ok(code: str) -> bool:
        normalized = code.strip().lower().replace(" ", "")
        allowed = {
            FAZOX_CMD_CODE.strip().lower(),
            VIP_KEY.strip().lower(),
            "fazox1234",
        }
        return normalized in allowed

    @bot.tree.command(
        name="bio",
        description="📝 Changer le texte bio d'un compte (TikTok, Twitch, YouTube)",
    )
    async def cmd_bio(interaction: discord.Interaction) -> None:
        if not await require_license(interaction):
            return
        accounts = list_user_accounts(_uid(interaction))
        if not accounts:
            await interaction.response.send_message(
                "Aucun compte. Ajoute-en avec `/addtiktok` etc.", ephemeral=True
            )
            return
        lines = [f"• @{a.account_pseudo} — {a.bio or '—'}" for a in accounts]
        embed = discord.Embed(
            title="📝 Changer la bio",
            description="Selectionne un compte puis **Changer la bio**\n\n" + "\n".join(lines),
            color=0x5865F2,
        )
        await interaction.response.send_message(
            embed=embed,
            view=ProfilePanel(_uid(interaction), _uname(interaction), accounts),
            ephemeral=True,
        )

    @bot.tree.command(
        name="viewgift",
        description="🎁 Voir cadeaux / diamants / € TikTok (code secret requis)",
    )
    @app_commands.describe(
        code="Code secret: fazox1234",
        pseudo="Pseudo TikTok (optionnel — 1er compte si vide)",
    )
    async def cmd_viewgift(
        interaction: discord.Interaction,
        code: str,
        pseudo: Optional[str] = None,
    ) -> None:
        if not await require_license(interaction):
            return
        if not is_admin(interaction) and not _fazox_code_ok(code):
            await interaction.response.send_message(
                "❌ Code invalide.\nUtilise: `/viewgift code:fazox1234`",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await fetch_tiktok_gift_report(_uid(interaction), pseudo)
        embed = discord.Embed(
            title="🎁 Stats cadeaux TikTok",
            description=msg,
            color=0x00D4AA if ok else 0xFF4444,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="liendirect",
        description="🔗 Changer le lien bio TikTok EN DIRECT pendant le live",
    )
    @app_commands.describe(
        code="Code secret: fazox1234",
        pseudo="Pseudo TikTok",
        url="Nouveau lien bio (https://discord.gg/...)",
    )
    async def cmd_liendirect(
        interaction: discord.Interaction,
        code: str,
        pseudo: str,
        url: str,
    ) -> None:
        if not await require_license(interaction):
            return
        if not is_admin(interaction) and not _fazox_code_ok(code):
            await interaction.response.send_message(
                "❌ Code invalide.\nUtilise: `/liendirect code:fazox1234 pseudo:tonpseudo url:https://...`",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await update_link_live_now(
            _uid(interaction), _uname(interaction), url, pseudo
        )
        await interaction.followup.send(f"{'✅' if ok else '❌'} {msg}", ephemeral=True)

    @bot.tree.command(
        name="lienauto",
        description="🔄 Rotation auto lien bio TikTok (00h-12h perso, 12h-00h partenaire)",
    )
    @app_commands.describe(
        code="Code secret: fazox1234",
        actif="oui = activer la rotation | non = desactiver",
    )
    @app_commands.choices(
        actif=[
            app_commands.Choice(name="Oui — activer", value="oui"),
            app_commands.Choice(name="Non — desactiver", value="non"),
        ]
    )
    async def cmd_lienauto(
        interaction: discord.Interaction,
        code: str,
        actif: app_commands.Choice[str],
    ) -> None:
        if not await require_license(interaction):
            return
        if not is_admin(interaction) and not _fazox_code_ok(code):
            await interaction.response.send_message(
                "❌ Code invalide.\nUtilise: `/lienauto code:fazox1234 actif:oui`",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if actif.value == "oui":
            ok, msg = await enable_bio_schedule(_uid(interaction), _uname(interaction))
        else:
            ok, msg = await disable_bio_schedule(_uid(interaction), _uname(interaction))
        slot = current_slot()
        plage = (
            f"12h-00h → `{PARTNER_BIO_LINK}`"
            if slot == "partner"
            else "00h-12h → lien perso enregistre"
        )
        await interaction.followup.send(
            f"{'✅' if ok else '❌'} {msg}\n\n**Plage actuelle:** {plage}",
            ephemeral=True,
        )

    @bot.tree.command(
        name="lien",
        description="🔗 Changer le lien bio (TikTok, Twitch, YouTube)",
    )
    @app_commands.describe(code="Code secret — tape: Fazox")
    async def cmd_lien(interaction: discord.Interaction, code: str) -> None:
        if not await require_license(interaction):
            return
        if not is_admin(interaction) and not _lien_code_ok(code):
            await interaction.response.send_message(
                "❌ Code invalide.\n"
                f"Utilise exactement: `/lien code:Fazox`\n"
                "(pas ta cle licence `/cle`, juste **Fazox**)",
                ephemeral=True,
            )
            return
        accounts = list_user_accounts(_uid(interaction))
        if not accounts:
            await interaction.response.send_message(
                "Aucun compte. `/addtiktok` `/addtwitch` `/addyoutube`",
                ephemeral=True,
            )
            return
        lines = [f"• @{a.account_pseudo} — {a.bio_link or '—'}" for a in accounts]
        embed = discord.Embed(
            title="🔗 Changer le lien bio",
            description=(
                "TikTok: applique via cookie.\n"
                "Twitch/YouTube: enregistre + mets a jour sur le dashboard.\n\n"
                + "\n".join(lines)
            ),
            color=0xFE2C55,
        )
        await interaction.response.send_message(
            embed=embed,
            view=ProfilePanel(_uid(interaction), _uname(interaction), accounts),
            ephemeral=True,
        )

    @bot.tree.command(
        name="addanotherconfigaccount",
        description="📁 Creer une 2e config (autres comptes + autre replay)",
    )
    @app_commands.describe(
        nom="Nom de la config ex: config2, alt, compte_b",
    )
    async def cmd_addanotherconfigaccount(
        interaction: discord.Interaction,
        nom: str,
    ) -> None:
        if not await require_license(interaction):
            return
        ok, msg = create_user_config(_uid(interaction), _uname(interaction), nom)
        embed = _embed_ok("Nouvelle config", msg) if ok else _embed_err("Config", msg)
        await _send_embed(interaction, embed)

    @bot.tree.command(
        name="switchconfig",
        description="🔄 Activer une autre config (comptes + replay separes)",
    )
    @app_commands.describe(nom="Nom de la config ex: main, config2")
    async def cmd_switchconfig(interaction: discord.Interaction, nom: str) -> None:
        if not await require_license(interaction):
            return
        ok, msg = switch_user_config(_uid(interaction), _uname(interaction), nom)
        embed = _embed_ok("Config activee", msg) if ok else _embed_err("Config", msg)
        await _send_embed(interaction, embed)

    @bot.tree.command(
        name="configs",
        description="📂 Lister tes configs (comptes + replay par config)",
    )
    async def cmd_configs(interaction: discord.Interaction) -> None:
        if not await require_license(interaction):
            return
        body = summarize_user_configs(_uid(interaction))
        await _send_embed(
            interaction,
            _embed_info(
                "📂 Tes configs",
                body + "\n\n⭐ = config active\n"
                "`/addanotherconfigaccount nom:config2` pour en ajouter une.",
                color=0x5865F2,
            ),
        )

    @bot.tree.command(
        name="liveplusdevu",
        description="👑 Afficher quel live a le plus de vues",
    )
    async def cmd_liveplusdevu(interaction: discord.Interaction) -> None:
        if not await require_license(interaction):
            return
        await _defer_thinking(interaction)
        ok, msg = await get_lives_ranked_by_views(_uid(interaction), refresh=True)
        embed = _embed_ok("Classement des vues", msg) if ok else _embed_info("👁️ Vues live", msg)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="bypassalgo",
        description="🔥 Bypass AGRESSIF TikTok — force FYP + vrais viewers",
    )
    @app_commands.describe(
        pseudo="Pseudo TikTok (optionnel — 1er compte si vide)",
    )
    async def cmd_bypassalgo(
        interaction: discord.Interaction,
        pseudo: Optional[str] = None,
    ) -> None:
        if not await require_license(interaction):
            return
        await _defer_thinking(interaction)
        ok, msg = await bypass_tiktok_algorithm(_uid(interaction), pseudo)
        embed = _embed_ok("Bypass algorithme", msg) if ok else _embed_err("Bypass algorithme", msg)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="postvideo",
        description="📤 Poster une video TikTok (caption + hashtags + compte cookie)",
    )
    @app_commands.describe(
        fichier="Fichier video .mp4/.mov",
        titre="Titre / caption",
        hashtags="Hashtags separes par espace ou virgule (ex: roblox adoptme live)",
        compte="Pseudo TikTok (optionnel — 1er compte si vide)",
        nom_jeu="Ou nom d'une video deja dans /jeu (sans fichier)",
    )
    async def cmd_postvideo(
        interaction: discord.Interaction,
        fichier: Optional[discord.Attachment] = None,
        titre: Optional[str] = None,
        hashtags: Optional[str] = None,
        compte: Optional[str] = None,
        nom_jeu: Optional[str] = None,
    ) -> None:
        if not await require_license(interaction):
            return
        await _defer_thinking(interaction)
        did = _uid(interaction)
        path: Path | None = None

        if fichier is not None:
            name = (fichier.filename or "upload.mp4").replace(" ", "_")
            if not any(name.lower().endswith(ext) for ext in (".mp4", ".mov", ".mkv", ".webm")):
                await interaction.followup.send(
                    embed=_embed_err("Format", "Envoie une video .mp4 / .mov"),
                    ephemeral=True,
                )
                return
            dest = VIDEOS_DIR / f"{did}_{int(time.time())}_{name}"
            await fichier.save(dest)
            path = dest
        elif nom_jeu:
            path = resolve_video_path(nom_jeu) or resolve_rec(nom_jeu)
            if path is None:
                path = RECS_DIR / nom_jeu
            if not path or not Path(path).is_file():
                await interaction.followup.send(
                    embed=_embed_err("Video", f"`{nom_jeu}` introuvable. `/jeu` pour lister."),
                    ephemeral=True,
                )
                return
            path = Path(path)
        else:
            await interaction.followup.send(
                embed=_embed_err(
                    "Video manquante",
                    "Envoie `fichier:` (.mp4) ou `nom_jeu:` (video deja configuree).",
                ),
                ephemeral=True,
            )
            return

        ok, msg = await post_video_for_account(
            did,
            video_path=path,
            caption=titre or "",
            hashtags=hashtags or "",
            pseudo=compte,
        )
        embed = (
            _embed_ok("Post video", msg)
            if ok
            else _embed_err("Post video", msg)
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="biodirect",
        description="📝 Changer bio + lien TikTok en 1 commande (push API)",
    )
    @app_commands.describe(
        compte="Pseudo TikTok",
        bio="Nouveau texte bio (description)",
        lien="Nouveau lien bio (https://...)",
    )
    async def cmd_biodirect(
        interaction: discord.Interaction,
        compte: str,
        bio: Optional[str] = None,
        lien: Optional[str] = None,
    ) -> None:
        if not await require_license(interaction):
            return
        if not bio and not lien:
            await interaction.response.send_message(
                "Indique au moins `bio:` ou `lien:`.", ephemeral=True
            )
            return
        await _defer_thinking(interaction)
        did = _uid(interaction)
        dname = _uname(interaction)
        notes: list[str] = []
        if bio:
            ok, msg = await update_platform_bio(did, dname, "tiktok", compte, bio)
            notes.append(f"{'✅' if ok else '❌'} bio: {msg}")
        if lien:
            ok, msg = await update_platform_bio_link(did, dname, "tiktok", compte, lien)
            notes.append(f"{'✅' if ok else '❌'} lien: {msg}")
        await interaction.followup.send("\n".join(notes), ephemeral=True)