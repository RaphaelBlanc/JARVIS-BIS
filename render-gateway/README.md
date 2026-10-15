# render-gateway

Composant **render-gateway** de l'assistant personnel.
Serveur FastAPI déployé sur [Render](https://render.com) qui fait le pont entre les bots Telegram/Discord et le **home-runner** (PC maison exposé via Cloudflare Tunnel).

```
[Telegram / Discord] ──► [render-gateway (Render)] ──► [home-runner (PC maison)]
                                                              │
                                                         Cloudflare Tunnel
```

---

## Variables d'environnement

Copiez `.env.example` en `.env` et renseignez les valeurs.

| Variable | Requis | Description |
|---|---|---|
| `HOME_SERVER_URL` | ✅ | URL publique du home-runner (ex: `https://xyz.trycloudflare.com`) |
| `SECRET_KEY` | ✅ | Clé secrète partagée avec le home-runner |
| `TELEGRAM_TOKEN` | ✅ | Token du bot Telegram (via @BotFather) |
| `TELEGRAM_WEBHOOK_URL` | ✅ | URL publique de ce service Render (ex: `https://mon-assistant.onrender.com`) |
| `DISCORD_TOKEN` | ⚠️ optionnel | Token du bot Discord |
| `DISCORD_GUILD_ID` | ⚠️ optionnel | ID du serveur Discord (slash commands en dev) |
| `TELEGRAM_ALLOWED_USER_IDS` | ⚠️ optionnel | IDs Telegram autorisés, séparés par virgule. Vide = tout le monde |
| `DISCORD_ALLOWED_USER_IDS` | ⚠️ optionnel | IDs Discord autorisés, séparés par virgule. Vide = tout le monde |

---

## Déploiement sur Render

1. Poussez ce dossier sur un dépôt GitHub/GitLab.
2. Sur [render.com](https://render.com), créez un **New Web Service** et pointez sur le dépôt.
3. Render détecte automatiquement le fichier [`render.yaml`](render.yaml).
4. Ajoutez les variables d'environnement dans le dashboard Render (onglet *Environment*).
5. Déployez. L'URL publique sera de la forme `https://<nom>.onrender.com`.

> **Important** : copiez l'URL Render dans `TELEGRAM_WEBHOOK_URL` avant le premier démarrage.

---

## Configurer le bot Telegram

1. Ouvrez Telegram et démarrez une conversation avec [@BotFather](https://t.me/BotFather).
2. Envoyez `/newbot` et suivez les instructions pour créer votre bot.
3. Copiez le token dans la variable `TELEGRAM_TOKEN`.
4. Le webhook est enregistré automatiquement au démarrage du service.
5. Pour vérifier : `GET https://api.telegram.org/bot<TOKEN>/getWebhookInfo`

### Commandes disponibles

| Commande | Description |
|---|---|
| `/start` | Message de bienvenue |
| `/help` | Affiche l'aide |
| _Tout autre texte_ | Transmis au home-runner |

---

## Configurer le bot Discord

1. Allez sur [discord.com/developers/applications](https://discord.com/developers/applications).
2. Créez une nouvelle application → onglet **Bot** → copiez le token dans `DISCORD_TOKEN`.
3. Activez l'intent **Message Content** dans les paramètres du bot.
4. Invitez le bot sur votre serveur avec les permissions `Send Messages` + `Use Slash Commands`.
5. (Dev) Renseignez `DISCORD_GUILD_ID` pour que les slash commands soient disponibles immédiatement.

### Commandes disponibles

| Commande | Description |
|---|---|
| `/ask <question>` | Slash command — pose une question à l'assistant |
| `!ask <question>` | Commande préfixée — même fonctionnement |

---

## Tester en local

```bash
# 1. Cloner et installer les dépendances
cd render-gateway
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configurer l'environnement
cp .env.example .env
# Éditez .env avec vos valeurs réelles

# 3. Démarrer le serveur
uvicorn main:app --reload --port 8000

# 4. Tester les routes de santé
curl http://localhost:8000/ping
curl http://localhost:8000/health

# 5. Exposer le port local pour Telegram (webhook)
# Utilisez ngrok ou cloudflared:
cloudflared tunnel --url http://localhost:8000
# → Mettez l'URL obtenue dans TELEGRAM_WEBHOOK_URL et redémarrez
```

---

## Structure du projet

```
render-gateway/
├── main.py              # Serveur FastAPI principal
├── gateway.py           # Client HTTP vers le home-runner
├── bots/
│   ├── __init__.py
│   ├── telegram_bot.py  # Bot Telegram (webhook)
│   └── discord_bot.py   # Bot Discord (thread daemon)
├── requirements.txt
├── render.yaml          # Config déploiement Render
├── Dockerfile           # Image Docker Python 3.12 slim
└── .env.example         # Template des variables d'environnement
```
