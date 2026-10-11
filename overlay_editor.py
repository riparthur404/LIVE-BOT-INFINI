"""Editeur interactif /custom — deplacer QR code et overlays a la main."""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import Database
from overlay_config import (
    CANVAS_H,
    CANVAS_W,
    ImageOverlay,
    OverlayConfig,
    TextOverlay,
    draft_from_session,
    draft_to_session_dict,
    load_config,
    save_config,
    user_assets_dir,
)
from overlay_render import (
    cache_video_frame,
    invalidate_frame_cache,
    overlay_png_path,
    preview_path,
    render_overlay_png,
    render_preview_fast,
)
from qr_tools import generate_qr_image, normalize_qr_url
from rec_library import resolve_video_path

log = logging.getLogger(__name__)
db = Database()

MOVE_STEP = 30
FINE_STEP = 10
RESIZE_STEP = 25
QR_DEFAULT = ImageOverlay(filename="qr.png", x=780, y=1560, width=220, height=220)


def active_overlay_config(discord_id: str) -> OverlayConfig:
    sess = db.get_session(discord_id)
    name = (sess.get("overlay_config_name") or "").strip()
    if name:
        loaded = load_config(discord_id, name)
        if loaded:
            return loaded
    return draft_from_session(sess)


def save_overlay_draft(discord_id: str, discord_name: str, config: OverlayConfig) -> None:
    db.save_session(discord_id, discord_name, **draft_to_session_dict(config))


def _video_for_preview(discord_id: str) -> Optional[Path]:
    sess = db.get_session(discord_id)
    video = sess.get("video_path") or ""
    if video and Path(video).is_file():
        return Path(video)
    return resolve_video_path(video)


def _clamp_image(item: ImageOverlay) -> None:
    item.width = max(40, min(CANVAS_W, item.width))
    item.height = max(40, min(CANVAS_H, item.height))
    item.x = max(0, min(CANVAS_W - item.width, item.x))
    item.y = max(0, min(CANVAS_H - item.height, item.y))


def _clamp_text(item: TextOverlay) -> None:
    item.x = max(0, min(CANVAS_W - 40, item.x))
    item.y = max(0, min(CANVAS_H - 40, item.y))


def _element_label(kind: str, index: int, cfg: OverlayConfig) -> str:
    if kind == "img":
        img = cfg.images[index]
        name = img.filename or f"image-{index + 1}"
        if "qr" in name.lower():
            name = f"QR ({name})"
        return f"🖼️ {name}"
    text = cfg.texts[index]
    preview = (text.content[:18] + "…") if len(text.content) > 18 else text.content
    return f"📝 {preview or f'texte-{index + 1}'}"


def list_elements(cfg: OverlayConfig) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for i in range(len(cfg.images)):
        out.append(("img", i, _element_label("img", i, cfg)))
    for i in range(len(cfg.texts)):
        out.append(("text", i, _element_label("text", i, cfg)))
    return out


def parse_element_id(value: str) -> tuple[str, int]:
    kind, raw = value.split(":", 1)
    return kind, int(raw)


def move_element(cfg: OverlayConfig, kind: str, index: int, dx: int, dy: int) -> bool:
    if kind == "img":
        if index >= len(cfg.images):
            return False
        item = cfg.images[index]
        item.x += dx
        item.y += dy
        _clamp_image(item)
        return True
    if kind == "text":
        if index >= len(cfg.texts):
            return False
        item = cfg.texts[index]
        item.x += dx
        item.y += dy
        _clamp_text(item)
        return True
    return False


def resize_element(cfg: OverlayConfig, kind: str, index: int, delta: int) -> bool:
    if kind != "img" or index >= len(cfg.images):
        return False
    item = cfg.images[index]
    item.width = max(40, item.width + delta)
    item.height = max(40, item.height + delta)
    _clamp_image(item)
    return True


async def _save_qr_attachment(discord_id: str, attachment: discord.Attachment) -> Optional[str]:
    if not attachment.filename:
        return None
    ext = Path(attachment.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        return None
    dest_name = "qr.png" if ext == ".png" else f"qr{ext}"
    dest = user_assets_dir(discord_id) / dest_name
    await attachment.save(dest)
    return dest_name


def upsert_text(cfg: OverlayConfig, content: str) -> TextOverlay:
    text = (content or "").strip()
    if not text:
        raise ValueError("Texte vide.")
    item = TextOverlay(content=text, x=40, y=80, font_size=52)
    cfg.texts.append(item)
    return item


def upsert_qr_image(cfg: OverlayConfig, filename: str) -> ImageOverlay:
    for img in cfg.images:
        if img.filename == filename or "qr" in img.filename.lower():
            img.filename = filename
            return img
    item = ImageOverlay(
        filename=filename,
        x=QR_DEFAULT.x,
        y=QR_DEFAULT.y,
        width=QR_DEFAULT.width,
        height=QR_DEFAULT.height,
    )
    cfg.images.insert(0, item)
    return item


def _preview_output_path(discord_id: str, cfg: OverlayConfig) -> Path:
    return preview_path(discord_id, cfg.name or "custom_edit")


async def _reply_after_defer(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
) -> None:
    """Repond apres defer() — compatible PebbleHost."""
    kwargs: dict = {}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if interaction.response.is_done():
        await interaction.edit_original_response(**kwargs)
    else:
        await interaction.response.send_message(**kwargs, ephemeral=True)


async def render_editor_preview_async(
    discord_id: str,
    cfg: OverlayConfig,
    *,
    full: bool = False,
) -> Path:
    """Apercu editeur — ffmpeg une seule fois (ou sur demande avec full=True)."""
    video = _video_for_preview(discord_id)
    out = _preview_output_path(discord_id, cfg)
    if full:
        invalidate_frame_cache(discord_id, video)
        await asyncio.to_thread(cache_video_frame, discord_id, video)
    return await asyncio.to_thread(render_preview_fast, cfg, discord_id, video, out)


def persist_overlay(discord_id: str, discord_name: str, cfg: OverlayConfig, name: str) -> str:
    cfg.name = name.strip() or "custom"
    save_config(discord_id, cfg)
    save_overlay_draft(discord_id, discord_name, cfg)
    render_overlay_png(cfg, discord_id, overlay_png_path(discord_id, cfg.name))
    db.save_session(
        discord_id,
        discord_name,
        overlay_config_name=cfg.name,
        overlay_draft=cfg.to_dict(),
    )
    return cfg.name


class CustomSaveModal(discord.ui.Modal, title="Sauvegarder ta config live"):
    nom = discord.ui.TextInput(
        label="Nom de la config",
        placeholder="ex: mon-live-qr",
        default="custom",
        required=True,
        max_length=40,
    )

    def __init__(self, discord_id: str, discord_name: str, view: "CustomEditorView") -> None:
        super().__init__(timeout=300)
        self.discord_id = discord_id
        self.discord_name = discord_name
        self.editor_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.discord_id:
            await interaction.response.send_message("Ce panneau ne t'appartient pas.", ephemeral=True)
            return
        cfg = self.editor_view.cfg
        if not cfg.texts and not cfg.images:
            await interaction.response.send_message("❌ Rien a sauvegarder.", ephemeral=True)
            return
        saved = persist_overlay(self.discord_id, self.discord_name, cfg, self.nom.value.strip())
        await interaction.response.send_message(
            f"✅ **Config `{saved}` sauvegardee !**\n"
            f"Lance `/startall` — ton QR code sera sur le live.",
            ephemeral=True,
        )


class ElementSelect(discord.ui.Select):
    def __init__(self, cfg: OverlayConfig, selected: str) -> None:
        elements = list_elements(cfg)
        options = [
            discord.SelectOption(label=label[:100], value=f"{kind}:{idx}", default=(f"{kind}:{idx}" == selected))
            for kind, idx, label in elements[:25]
        ]
        if not options:
            options = [discord.SelectOption(label="Aucun element — ajoute un QR", value="none:none")]
        super().__init__(placeholder="Choisir quoi deplacer (QR, texte...)", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: CustomEditorView = self.view  # type: ignore[assignment]
        if not await view._only_owner(interaction):
            return
        if self.values[0] == "none:none":
            await interaction.response.send_message(
                "Ajoute un QR: `/qr lien:https://...` ou `/custom qr:[fichier]`", ephemeral=True
            )
            return
        view.selected = self.values[0]
        await view.refresh(interaction, notice=f"Selection: **{self.values[0]}**", update_image=False)


class CustomEditorView(discord.ui.View):
    def __init__(self, discord_id: str, discord_name: str, cfg: OverlayConfig) -> None:
        super().__init__(timeout=900)
        self.discord_id = discord_id
        self.discord_name = discord_name
        self.cfg = cfg
        elements = list_elements(cfg)
        self.selected = f"{elements[0][0]}:{elements[0][1]}" if elements else "img:0"
        self._rebuild_items()

    def _rebuild_items(self) -> None:
        """Rafraichit seulement le menu — garde les boutons fleches."""
        for child in list(self.children):
            if isinstance(child, ElementSelect):
                self.remove_item(child)
        self.add_item(ElementSelect(self.cfg, self.selected))

    async def _only_owner(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.discord_id:
            await interaction.response.send_message("Ce panneau ne t'appartient pas.", ephemeral=True)
            return False
        return True

    def _current_element(self) -> tuple[str, int] | None:
        elements = list_elements(self.cfg)
        if not elements:
            return None
        try:
            return parse_element_id(self.selected)
        except (ValueError, IndexError):
            kind, idx, _ = elements[0]
            return kind, idx

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        log.exception("CustomEditorView (%s): %s", item, error)
        msg = "❌ Erreur editeur — reessaie `/custom`."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _ack_interaction(self, interaction: discord.Interaction) -> None:
        """Repond a Discord en <3s pour eviter « Echec de l'interaction »."""
        if interaction.response.is_done():
            return
        await interaction.response.defer()

    async def _edit_panel(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        preview: Path | None = None,
    ) -> None:
        """Met a jour le message de l'editeur (boutons / menu)."""
        self._rebuild_items()
        kwargs: dict = dict(embed=embed, view=self)

        if preview is not None:
            preview_bytes = await asyncio.to_thread(preview.read_bytes)
            kwargs["attachments"] = [discord.File(io.BytesIO(preview_bytes), filename="apercu_custom.jpg")]

        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(**kwargs)
            else:
                await interaction.response.edit_message(**kwargs)
        except discord.HTTPException:
            if interaction.message is not None:
                await interaction.message.edit(**kwargs)
            else:
                raise

    def _build_embed(self, *, notice: str = "") -> discord.Embed:
        el = self._current_element()
        pos = ""
        if el:
            kind, idx = el
            if kind == "img" and idx < len(self.cfg.images):
                img = self.cfg.images[idx]
                pos = f"QR/image @ **({img.x}, {img.y})** — {img.width}x{img.height}px"
            elif kind == "text" and idx < len(self.cfg.texts):
                t = self.cfg.texts[idx]
                pos = f"Texte @ **({t.x}, {t.y})**"

        desc = (
            "**Deplace image ou texte avec les fleches**\n"
            "• Gros pas: ⬆️⬇️⬅️➡️ | Petits pas: 🔼🔽◀️▶️\n"
            "Puis clique **Sauvegarder** — c'est bon pour le live.\n\n"
        )
        if pos:
            desc += f"📍 {pos}\n"
        if notice:
            desc += f"\n{notice}"

        embed = discord.Embed(
            title="🎨 Custom Live — editeur",
            description=desc,
            color=0x9B59B6,
        )
        embed.set_image(url="attachment://apercu_custom.jpg")
        embed.set_footer(
            text="Fleches = deplacer | 🔄 Apercu = rafraichir l'image | 💾 Sauvegarder"
        )
        return embed

    async def refresh(
        self,
        interaction: discord.Interaction,
        *,
        notice: str = "",
        full: bool = False,
        update_image: bool = True,
    ) -> None:
        await self._ack_interaction(interaction)

        try:
            await asyncio.to_thread(save_overlay_draft, self.discord_id, self.discord_name, self.cfg)
            embed = self._build_embed(notice=notice)
            preview: Path | None = None
            if update_image:
                preview = await render_editor_preview_async(self.discord_id, self.cfg, full=full)
            await self._edit_panel(interaction, embed=embed, preview=preview)
        except Exception:
            log.exception("Echec refresh editeur user=%s", self.discord_id)
            try:
                await interaction.followup.send(
                    "❌ Erreur editeur — reouvre `/custom`.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

    async def _nudge(self, interaction: discord.Interaction, dx: int, dy: int) -> None:
        if not await self._only_owner(interaction):
            return
        el = self._current_element()
        if not el:
            await interaction.response.send_message(
                "❌ Ajoute un QR: `/custom qr:[ton image]`", ephemeral=True
            )
            return
        kind, idx = el
        if not move_element(self.cfg, kind, idx, dx, dy):
            await interaction.response.send_message("❌ Element introuvable.", ephemeral=True)
            return
        await self.refresh(interaction, update_image=False)

    @discord.ui.button(label="⬆️ Haut", style=discord.ButtonStyle.secondary, row=1)
    async def up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._nudge(interaction, 0, -MOVE_STEP)

    @discord.ui.button(label="⬇️ Bas", style=discord.ButtonStyle.secondary, row=1)
    async def down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._nudge(interaction, 0, MOVE_STEP)

    @discord.ui.button(label="⬅️ Gauche", style=discord.ButtonStyle.secondary, row=1)
    async def left(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._nudge(interaction, -MOVE_STEP, 0)

    @discord.ui.button(label="➡️ Droite", style=discord.ButtonStyle.secondary, row=1)
    async def right(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._nudge(interaction, MOVE_STEP, 0)

    @discord.ui.button(label="🔼", style=discord.ButtonStyle.primary, row=2)
    async def fine_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._nudge(interaction, 0, -FINE_STEP)

    @discord.ui.button(label="🔽", style=discord.ButtonStyle.primary, row=2)
    async def fine_down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._nudge(interaction, 0, FINE_STEP)

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.primary, row=2)
    async def fine_left(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._nudge(interaction, -FINE_STEP, 0)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.primary, row=2)
    async def fine_right(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._nudge(interaction, FINE_STEP, 0)

    @discord.ui.button(label="🔍+", style=discord.ButtonStyle.primary, row=3)
    async def bigger(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._only_owner(interaction):
            return
        el = self._current_element()
        if not el or el[0] != "img":
            await interaction.response.send_message("❌ Selectionne une image/QR.", ephemeral=True)
            return
        resize_element(self.cfg, el[0], el[1], RESIZE_STEP)
        await self.refresh(interaction, notice="Taille augmentee", update_image=False)

    @discord.ui.button(label="🔍-", style=discord.ButtonStyle.primary, row=3)
    async def smaller(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._only_owner(interaction):
            return
        el = self._current_element()
        if not el or el[0] != "img":
            await interaction.response.send_message("❌ Selectionne une image/QR.", ephemeral=True)
            return
        resize_element(self.cfg, el[0], el[1], -RESIZE_STEP)
        await self.refresh(interaction, notice="Taille reduite", update_image=False)

    @discord.ui.button(label="💾 Sauvegarder", style=discord.ButtonStyle.success, row=4)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._only_owner(interaction):
            return
        if not self.cfg.texts and not self.cfg.images:
            await interaction.response.send_message("❌ Rien a sauvegarder.", ephemeral=True)
            return
        await interaction.response.send_modal(CustomSaveModal(self.discord_id, self.discord_name, self))

    @discord.ui.button(label="🔄 Apercu", style=discord.ButtonStyle.secondary, row=4)
    async def preview_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._only_owner(interaction):
            return
        await self.refresh(interaction, notice="Apercu actualise", full=True, update_image=True)


async def open_custom_editor(
    interaction: discord.Interaction,
    *,
    qr: Optional[discord.Attachment] = None,
    lien: Optional[str] = None,
    texte: Optional[str] = None,
    nom: Optional[str] = None,
) -> None:
    did = str(interaction.user.id)
    dname = interaction.user.display_name or did
    cfg = active_overlay_config(did)

    if nom:
        loaded = load_config(did, nom.strip())
        if loaded:
            cfg = loaded

    if lien:
        norm, url_err = normalize_qr_url(lien)
        if url_err or not norm:
            await _reply_after_defer(interaction, content=f"❌ {url_err or 'Lien invalide.'}")
            return
        try:
            path = generate_qr_image(did, norm, size=QR_DEFAULT.width, name="qr")
            upsert_qr_image(cfg, path.name)
            save_overlay_draft(did, dname, cfg)
        except (ValueError, OSError) as exc:
            await _reply_after_defer(interaction, content=f"❌ QR impossible: {exc}")
            return

    if qr:
        fname = await _save_qr_attachment(did, qr)
        if not fname:
            await _reply_after_defer(
                interaction,
                content="❌ Format invalide — envoie un **PNG** ou **JPG**.",
            )
            return
        upsert_qr_image(cfg, fname)
        save_overlay_draft(did, dname, cfg)

    if texte:
        try:
            upsert_text(cfg, texte)
            save_overlay_draft(did, dname, cfg)
        except ValueError as exc:
            await _reply_after_defer(interaction, content=f"❌ {exc}")
            return

    if not cfg.texts and not cfg.images:
        await _reply_after_defer(
            interaction,
            content=(
                "❌ Aucun element sur le live.\n"
                "Ajoute un QR ou un texte:\n"
                "`/custom lien:https://discord.gg/xxx`\n"
                "`/custom qr:[image]` ou `/custom texte:Mon texte`"
            ),
        )
        return

    video = _video_for_preview(did)
    await asyncio.to_thread(cache_video_frame, did, video)
    preview = await asyncio.to_thread(
        render_preview_fast, cfg, did, video, _preview_output_path(did, cfg)
    )
    el = list_elements(cfg)
    first = el[0] if el else None
    pos = ""
    if first:
        kind, idx, label = first
        if kind == "img":
            img = cfg.images[idx]
            pos = f"**{label}** @ ({img.x}, {img.y})"
        else:
            t = cfg.texts[idx]
            pos = f"**{label}** @ ({t.x}, {t.y})"

    embed = discord.Embed(
        title="🎨 Custom Live — editeur",
        description=(
            "**Deplace image ou texte avec les fleches**\n"
            "• Gros pas: ⬆️⬇️⬅️➡️ | Petits pas: 🔼🔽◀️▶️\n"
            "• Taille image: 🔍+ / 🔍- | Menu = choisir l'element\n"
            "Clique **💾 Sauvegarder** quand c'est bon.\n\n"
            f"📍 {pos or '—'}"
        ),
        color=0x9B59B6,
    )
    embed.set_image(url="attachment://apercu_custom.jpg")
    embed.set_footer(text="Utilise le menu pour choisir QR ou texte")

    view = CustomEditorView(did, dname, cfg)
    preview_bytes = await asyncio.to_thread(preview.read_bytes)
    await interaction.edit_original_response(
        embed=embed,
        attachments=[discord.File(io.BytesIO(preview_bytes), filename="apercu_custom.jpg")],
        view=view,
    )


def register_custom_command(
    bot: commands.Bot,
    *,
    require_license: Callable,
) -> None:
    @bot.tree.command(
        name="custom",
        description="🎨 Personnaliser le live — fleches pour deplacer image/texte",
    )
    @app_commands.describe(
        lien="Lien URL — genere automatiquement un QR code",
        qr="Image QR code (PNG/JPG) a placer sur le live",
        texte="Texte a afficher sur le live (deplacable avec les fleches)",
        nom="Nom d'une config existante a modifier (optionnel)",
    )
    async def cmd_custom(
        interaction: discord.Interaction,
        lien: Optional[str] = None,
        qr: Optional[discord.Attachment] = None,
        texte: Optional[str] = None,
        nom: Optional[str] = None,
    ) -> None:
        if not await require_license(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_custom_editor(interaction, qr=qr, lien=lien, texte=texte, nom=nom)

    @bot.tree.command(
        name="qr",
        description="📱 Generer un QR code depuis un lien et l'ajouter au live",
    )
    @app_commands.describe(
        lien="URL du lien (ex: https://discord.gg/xxx ou discord.gg/xxx)",
    )
    async def cmd_qr(interaction: discord.Interaction, lien: str) -> None:
        if not await require_license(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await open_custom_editor(interaction, lien=lien)