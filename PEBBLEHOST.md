# Fazox Multi Live — Hebergement PebbleHost

## Prerequis panel PebbleHost

- **Type:** Bot Discord (Python)
- **Python:** 3.11+
- **ffmpeg** installe sur le serveur (`apt install ffmpeg` si VPS)

## Installation

1. Upload le zip `fazox-pebblehost.zip` sur PebbleHost (File Manager ou SFTP)
2. Extraire dans le dossier du bot
3. Verifier que `.env` contient:
   ```
   DISCORD_TOKEN=ton_token
   WEBHOOK_URL=ton_webhook
   VIP_KEY=Fazox1234
   ADMIN_IDS=ton_id_discord
   LIEN_SECRET_KEY=Fazox
   ```
4. **Startup command** dans le panel:
   ```bash
   python3 -u start.py
   ```
   Ou:
   ```bash
   bash pebblehost/start.sh
   ```
   **Important:** ne mets pas seulement `bot.py` comme fichier de demarrage — utilise `start.py` pour capturer les erreurs.

5. **Version Python:** passe a **3.11+** dans le panel (3.9 peut planter silencieusement).

## Structure

| Dossier | Role |
|---------|------|
| `data/` | Base SQLite (1 cle = 1 utilisateur, comptes isoles) |
| `recs/` | Videos .mp4 pour les lives |
| `logs/` | Logs bot |

## Multi-utilisateurs (cles licence)

- Chaque client active `/cle cle:FAZOX-...`
- Ses comptes sont lies a **son** Discord ID uniquement
- Un cookie ou pseudo deja pris par un autre client = refuse
- Plusieurs clients peuvent etre en live **en meme temps** (sessions separees)

## Commandes clients

| Commande | Description |
|----------|-------------|
| `/cle` | Activer la licence |
| `/addtiktok` | Cookie TikTok (.txt) |
| `/addtwitch` | Cle stream Twitch |
| `/addyoutube` | Cle stream YouTube |
| `/jeu` | Choisir la video |
| `/startall` | Live sur tous les comptes du client |
| `/stoplive` | Arreter son live |
| `/comptes` | Voir son panel |
| `/lien code:Fazox` | Lien bio (TikTok/Twitch/YouTube) |
| `/bio` | Texte bio |
| `/supprimer` | Retirer un compte |

## Admin

`/live admin key_generate` — generer des cles pour tes clients

## Securite

Si le zip a ete partage: **regenere le token Discord** et le webhook.