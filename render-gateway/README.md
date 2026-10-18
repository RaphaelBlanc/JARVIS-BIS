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
| `TELEGRAM_WEBHOOK_SECRET` | ⚠️ optionnel | Secret du webhook Telegram. Vide = dérivé automatiquement de `SECRET_KEY` |
| `MAX_RESPONSE_LENGTH` | ⚠️ optionnel | Longueur max d'une réponse avant troncature (défaut `50000`, `0` = illimité) |
| `TELEGRAM_ALLOWED_USER_IDS` | ⚠️ optionnel | IDs Telegram autorisés, séparés par virgule. Vide = **refus de tout le monde** (fail-closed) |
| `DISCORD_ALLOWED_USER_IDS` | ⚠️ optionnel | IDs Discord autorisés, séparés par virgule. Vide = **refus de tout le monde** (fail-closed) |
| `ALLOW_ALL_USERS` | ⚠️ optionnel | `1` pour autoriser tout le monde sans whitelist (non recommandé) |

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

### Commandes disponibles (Telegram)

| Commande | Aliases | Description |
|---|---|---|
| `/briefing` | `/brief` | Génère votre briefing matinal interactif |
| `/agenda` | `/cal` | Consulte votre agenda Google Workspace |
| `/tasks` | `/task`, `/todo` | Gère Google Tasks (`list`, `add`, `done`, `delete`) |
| `/meteo [ville]` | `/weather` | Météo mondiale en direct (ex: `/meteo Paris`) |
| `/search [requete]` | `/recherche` | Recherche sur le web avec DuckDuckGo |
| `/summarize [url]` | — | Résume un article ou une page web |
| `/models` | `/model` | Menu interactif de sélection des modèles |
| `/setmodel [id]` | — | Change le modèle actif directement |
| `/effort [level]` | — | Règle l'effort de réflexion (`low`, `medium`, `high`) |
| `/mode [mode]` | — | Règle le mode d'exécution d'Antigravity |
| `/permissions` | — | Règle la validation des actions sensibles |
| `/new` | `/clear` | Démarre une nouvelle conversation vierge |
| `/resume` | — | Liste ou recharge une conversation passée |
| `/delete` | `/del`, `/delconv` | Supprime une conversation passée (`<id>` ou `all`) |
| `/compact` | — | Compacte la mémoire de contexte |
| `/session` | — | Affiche les détails de la session en cours |
| `/status` | — | Statut global du serveur et d'Antigravity |
| `/usage` | `/quota` | Consommation de quotas et jetons |
| `/skills` | — | Liste les compétences MCP connectées |
| `/help` | — | Affiche le menu d'aide |
| _Tout autre texte_ | — | Transmis directement à l'assistant |

---

## Configurer le bot Discord

1. Allez sur [discord.com/developers/applications](https://discord.com/developers/applications).
2. Créez une nouvelle application → onglet **Bot** → copiez le token dans `DISCORD_TOKEN`.
3. Activez l'intent **Message Content** dans les paramètres du bot.
4. Invitez le bot sur votre serveur avec les permissions `Send Messages` + `Use Slash Commands`.
5. (Dev) Renseignez `DISCORD_GUILD_ID` pour que les slash commands soient disponibles immédiatement.

### Commandes disponibles (Discord)

Toutes les commandes sont utilisables en **slash command native** (`/...`) ou en **commande préfixée** (`!...`) :

| Slash Command | Commande préfixée | Description |
|---|---|---|
| `/briefing` | `!briefing` (alias `!brief`) | Déclenche le briefing matinal complet |
| `/ask [question]` | `!ask [question]` | Pose une question ou donne une consigne |
| `/agenda [periode]` | `!agenda [periode]` (alias `!cal`) | Consulte l'agenda Google (`aujourd'hui`, `demain`, `semaine`) |
| `/tasks [action] [texte]` | `!tasks [action] [texte]` | Gestion Google Tasks (`list`, `add`, `done`, `del`) |
| `/meteo [ville]` | `!meteo [ville]` (alias `!weather`) | Météo mondiale en temps réel |
| `/search [requete]` | `!search [requete]` (alias `!recherche`) | Recherche sur le web avec DuckDuckGo |
| `/summarize [url]` | `!summarize [url]` | Résumé intelligent d'une URL |
| `/models` | `!models` | Liste les modèles Antigravity disponibles |
| `/setmodel [model]` | `!setmodel [id]` | Change le modèle IA actif |
| `/effort [level]` | `!effort [level]` | Ajuste l'effort de réflexion (`low`, `medium`, `high`) |
| `/mode [mode]` | `!mode [mode]` | Règle le mode d'exécution (`auto`, `plan`, etc.) |
| `/permissions [policy]` | `!permissions [policy]` | Politique d'exécution des outils sensibles |
| `/new` | `!new` (alias `!clear`, `!reset`) | Réinitialise et nettoie le salon |
| `/resume [conversation]` | `!resume [id]` | Reprend une conversation passée (rejoue le chat) |
| `/delete [conversation]` | `!delete [id]` | Supprime une conversation ou `all` |
| `/compact` | `!compact` | Compacter l'historique actif |
| `/session` | `!session` | Affiche l'état de la session active |
| `/status` | `!status` | Diagnostic système et état des bots |
| `/usage` | `!usage` | Quotas et jetons consommés |
| `/skills` | `!skills` | Affiche les compétences actives |
| _N/A_ | `!help` | Affiche l'aide complète |
| _Chat direct_ | _Chat direct_ | Parlez à l'assistant sans préfixe |

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
