# 🤖 Assistant Personnel — AI Agent & Gateway

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white" alt="Python Version" />
  <img src="https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Discord.py-2.4-5865F2?logo=discord&logoColor=white" alt="Discord.py" />
  <img src="https://img.shields.io/badge/Telegram-python--telegram--bot-2CA5E0?logo=telegram&logoColor=white" alt="Telegram" />
  <img src="https://img.shields.io/badge/Engine-Antigravity%20CLI%20(agy)-black" alt="Antigravity" />
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License" />
</p>

> **Un assistant personnel IA autonome, ultra-rapide et connecté à votre quotidien.**  
> Pilotez votre machine, vos e-mails, votre agenda, vos tâches et le web directement depuis **Discord** et **Telegram**, propulsé par **Antigravity CLI** et les modèles de raisonnement Google Gemini.

---

## 🌟 Fonctionnalités Principales

- **🌅 Briefing Matinal Automatisé** :
  - Déclenché automatiquement chaque matin à **08h00** ou sur commande (`/briefing`).
  - Analyse synthétique : **Emploi du temps du jour**, **E-mails importants filtrés** (anti-spam), **Tâches Google Tasks**, **Météo locale** et **Actualités économiques clés**.
- **⚡ Réponses Ultra-Rapides & Mode Éco** :
  - Optimisé pour le chat mobile avec `gemini-3.8-flash-low` et effort de raisonnement direct pour des réponses complètes en moins de **15 à 20 secondes**.
- **💬 Intégration Bi-Plateforme Complète** :
  - **Discord** : Commandes slash natives (`/ask`, `/briefing`, `/model`, `/session`, `/clear`), accusé de réception instantané (évite les erreurs timeout 3s), réponses élégantes découpées en blocs formatés.
  - **Telegram** : Menus interactifs, boutons inline, mode webhook ou polling automatique sans configuration réseau.
- **🧠 Gestion de Contexte & Mémoire** :
  - Mémoire de conversation persistante avec suivi des tours de discussion.
  - Commutation de session ou de modèle à la volée sans redémarrer le serveur.
  - Possibilité de reprendre d'anciennes conversations passées ou de purger le chat.
- **📐 Support LaTeX & Formules Mathématiques** :
  - Rendu graphique automatique des équations de bloc (`$$...$$`) en images PNG nettes adaptées au mode sombre.
  - Conversion automatique des formules en ligne (`$...$`) en symboles mathématiques Unicode clairs (ex: `α`, `β`, `π`, `x²`, `√`, `∫`, `∈`, `ℝ`).
- **👁️ Support Multimodal & Analyse d'Images** :
  - Envoyez une photo, capture d'écran, schéma ou exercice directement sur Discord (`/ask [image]` ou upload direct) ou Telegram (envoi de photo avec légende).
  - Analyse visuelle complète assurée par le modèle multimodal Gemini via Antigravity CLI.
- **📋 Code Copiable en 1 Clic & Fichiers Attachés** :
  - Coloration syntaxique complète avec activation du bouton de copie natif Telegram et Discord.
  - Découpage intelligent (`smart_split`) évitant toute troncature au milieu d'un bloc de code.
  - Envoi automatique des extraits de code volumineux sous forme de fichiers téléchargeables (`.py`, `.js`, `.sh`, etc.).
- **🔌 Connecteurs MCP (Model Context Protocol)** :
  - 📅 **Google Agenda** : Lecture et création d'événements.
  - ✉️ **Gmail** : Tri et résumé intelligent des e-mails prioritaires.
  - 📋 **Google Tasks** : Gestion et suivi des listes de tâches.
  - 📁 **Google Drive** : Recherche et consultation de documents.
  - 🌤️ **Météo** & 🌐 **Recherche Web DuckDuckGo**.

---

## 🏗️ Architecture & Déploiement

Le projet supporte **deux modes de déploiement** selon vos besoins :

### Mode 1 : Serveur Autonome Tout-en-un (Recommandé) ⭐
Idéal sur un serveur personnel (mini-PC, Raspberry Pi, serveur Debian/Ubuntu) allumé 24/7.

```mermaid
flowchart LR
    subgraph Clients["📱 Interfaces Utilisateur"]
        D[Discord Client]
        T[Telegram Client]
    end

    subgraph Serveur["🖥️ Serveur Autonome (Tout-en-un)"]
        GW["render-gateway :8000\n(Bots Discord & Telegram)"]
        HR["home-runner :8080\n(API FastAPI)"]
        AGY["Antigravity CLI\n(Moteur IA agy)"]
        MCP["Outils MCP\n(Google, Tasks, Météo)"]

        GW -->|HTTP Local :8080| HR
        HR -->|Subprocess| AGY
        AGY --> MCP
    end

    D <-->|WebSocket Sortant| GW
    T <-->|Polling Sortant| GW
```

> [!TIP]
> **Zéro galère réseau** : Dans ce mode, **aucun port n'a besoin d'être ouvert** sur votre box Internet, aucun nom de domaine n'est requis, et le bot reste actif en permanence.

---

### Mode 2 : Architecture Hybride Cloud + PC Local
Si vous préférez héberger la passerelle sur Render et le moteur IA sur votre PC de bureau personnel :

```mermaid
flowchart LR
    Discord --> Render[render-gateway sur Render.com]
    Telegram --> Render
    Render -->|HTTPS| Cloudflare[Cloudflare Tunnel]
    Cloudflare -->|Localhost :8080| HomeRunner[home-runner sur PC Local]
    HomeRunner --> agy[Antigravity CLI]
```

---

## 📂 Structure du Répertoire

```text
assistant-perso/
├── deploy.sh                  # 🚀 Script de déploiement automatique en 1 commande
├── GEMINI.md                  # 🎯 Directives système pour l'assistant IA
├── .env.example               # ⚙️ Modèle de configuration globale
├── systemd/                   # 🐧 Units systemd pour lancement en tâche de fond
│   ├── assistant-runner.service
│   └── assistant-gateway.service
├── home-runner/               # 🏠 Moteur local & Bridge Antigravity CLI
│   ├── agy_bridge.py          # Gestionnaire de sessions, parsing et subprocess
│   ├── main.py                # API FastAPI privée (/run, /session, /models)
│   ├── requirements.txt
│   └── setup.sh
├── render-gateway/            # 🌐 Passerelle bots Discord & Telegram
│   ├── bots/
│   │   ├── discord_bot.py     # Bot Discord (commandes slash, gestion des flux)
│   │   └── telegram_bot.py    # Bot Telegram (polling/webhook, briefing)
│   ├── gateway.py             # Client HTTP asynchrone vers le runner
│   ├── main.py                # Serveur FastAPI & planificateur matinal
│   ├── Dockerfile
│   └── render.yaml
└── integrations/              # 🔌 Connecteurs & Authentification
    └── google-workspace/      # Scripts OAuth et connecteur Google Tasks MCP
        ├── setup_oauth.py
        └── tasks_mcp.py
```

---

## ⚡ Guide de Démarrage Rapide

### 1. Prérequis
- Un système Linux (Fedora, Debian, Ubuntu, etc.)
- Python ≥ 3.11
- [Antigravity CLI](https://github.com/google/antigravity) installé et authentifié (`agy login`)

### 2. Installation en une commande
Clonez le dépôt et lancez le script d'installation automatisé :

```bash
git clone https://github.com/VOTRE_UTILISATEUR/assistant-perso.git
cd assistant-perso
chmod +x deploy.sh
./deploy.sh
```

Le script s'occupe de :
1. Vérifier la présence de Python 3 et d'Antigravity CLI.
2. Créer les environnements virtuels Python (`.venv`) pour `home-runner` et `render-gateway`.
3. Installer l'ensemble des dépendances.
4. Générer automatiquement une `SECRET_KEY` cryptographique sécurisée.
5. Préparer les fichiers `.env`.

### 3. Configuration des clés
Éditez `render-gateway/.env` pour renseigner vos tokens de messagerie :

```dotenv
# Bot Telegram (obtenu via @BotFather)
TELEGRAM_TOKEN=123456789:ABCDEF...

# Bot Discord (obtenu via le portail développeur Discord)
DISCORD_TOKEN=MTE...

# Sécurité (fail-closed : sans ID autorisé, les bots refusent tout le monde)
DISCORD_ALLOWED_USER_IDS=123456789012345678
TELEGRAM_ALLOWED_USER_IDS=123456789

# Pour ouvrir l'accès sans whitelist (non recommandé) :
# ALLOW_ALL_USERS=1
```

### 4. Lancement en tâche de fond (Systemd)

Pour que votre assistant démarre automatiquement au boot du système et survive aux redémarrages :

```bash
# Copier les services dans systemd utilisateur
mkdir -p ~/.config/systemd/user/
cp systemd/*.service ~/.config/systemd/user/

# Recharger et activer les services
systemctl --user daemon-reload
systemctl --user enable --now assistant-runner assistant-gateway

# Vérifier que tout fonctionne
systemctl --user status assistant-runner assistant-gateway
```

Les journaux en temps réel sont consultables avec :
```bash
journalctl --user -u assistant-gateway -f
```

---

## 🎮 Guide Complet des Commandes

Toutes les fonctionnalités sont disponibles sur **Discord** (commandes slash natives `/` et préfixe `!`) et sur **Telegram** (`/`). Vous pouvez également poser vos questions en langage naturel directement dans le salon.

### 1. 📅 Quotidien & Productivité

| Commande Discord | Commande Telegram | Aliases | Description & Exemples |
|---|---|---|---|
| `/briefing` | `/briefing` | `/brief`, `!briefing` | Génère le briefing matinal complet (cours/agenda, mails filtrés, tâches, météo, actualités) |
| `/agenda [periode]` | `/agenda` | `/cal`, `!agenda` | Consulte votre Google Agenda (`aujourd'hui`, `demain`, `semaine`) |
| `/tasks [action] [texte]` | `/tasks` | `/task`, `/todo`, `!tasks` | Gère Google Tasks : `list`, `add <titre>`, `done <id>`, `delete <id>` |
| `/meteo [ville]` | `/meteo [ville]` | `/weather`, `!meteo` | Prévisions météo mondiales en temps réel (ex: `/meteo Lyon`) |
| `/search [requete]` | `/search [requete]` | `/recherche`, `!search` | Recherche sur le web via DuckDuckGo avec synthèse IA |
| `/summarize [url]` | `/summarize [url]` | `!summarize` | Extrait et résume le contenu d'un article ou d'une page web |

### 2. 🧠 Conversation, Sessions & Mémoire

| Commande Discord | Commande Telegram | Aliases | Description & Exemples |
|---|---|---|---|
| `/ask [question]` | _Message direct_ | `!ask [question]` | Pose une question ou confie une mission à l'assistant |
| `/new` | `/new` | `/clear`, `/reset`, `!new` | Démarre une nouvelle conversation vierge (nettoie le salon) |
| `/resume [conversation]` | `/resume` | `!resume [id]` | Affiche la liste des sessions récentes ou recharge une session passée |
| `/delete [conversation]` | `/delete` | `/del`, `/delconv`, `!delete` | Supprime une conversation passée (accepte un ID ou `all`) |
| `/compact` | `/compact` | `!compact` | Compacter l'historique de la session pour économiser des tokens |
| `/session` | `/session` | `!session` | Affiche l'ID actif, le modèle utilisé et le nombre d'échanges |

### 3. ⚙️ Modèles d'IA & Paramètres Moteur

| Commande Discord | Commande Telegram | Aliases | Description & Exemples |
|---|---|---|---|
| `/models` | `/models` | `/model`, `!models` | Liste les modèles Antigravity disponibles et le modèle actif |
| `/setmodel [model]` | `/setmodel [id]` | `!setmodel [id]` | Change le modèle IA (`gemini-3.8-flash-low`, `claude-sonnet-4-6`, etc.) |
| `/effort [level]` | `/effort [level]` | `!effort` | Ajuste l'effort de réflexion (`low`, `medium`, `high`, `xhigh`, `max`) |
| `/mode [mode]` | `/mode [mode]` | `!mode` | Règle le mode d'exécution (`auto`, `plan`, `accept-edits`) |
| `/permissions [policy]` | `/permissions` | `!permissions` | Règle la validation des actions (`always-proceed`, `request-review`) |

### 4. 📊 Diagnostic & Informations

| Commande Discord | Commande Telegram | Aliases | Description |
|---|---|---|---|
| `/status` | `/status` | `!status` | Statut global du système (santé du runner, état d'Antigravity CLI, session) |
| `/usage` | `/usage` | `/quota`, `!usage` | Affiche l'état des quotas de requêtes et de tokens |
| `/skills` | `/skills` | `!skills` | Affiche les compétences actives et serveurs MCP connectés |
| _N/A_ | `/start` | — | Initialise l'assistant sur Telegram avec le message d'accueil |
| `!help` | `/help` | — | Affiche le menu d'aide complet avec la liste des commandes |

---

## 🔒 Sécurité & Confidentialité

- **Authentification forte** : Chaque échange entre la passerelle (`render-gateway`) et le moteur local (`home-runner`) est protégé par une clé `X-Secret-Key` de 256 bits.
- **Accès restreint (Whitelist)** : Définissez `DISCORD_ALLOWED_USER_IDS` et `TELEGRAM_ALLOWED_USER_IDS` afin que personne d'autre ne puisse interagir avec votre assistant. Par défaut (**fail-closed**), une whitelist vide refuse tout le monde ; pour ouvrir l'accès sans whitelist, définissez explicitement `ALLOW_ALL_USERS=1` (non recommandé).
- **Webhook Telegram authentifié** : En mode webhook, chaque requête est validée via le header `X-Telegram-Bot-Api-Secret-Token`. Le secret est dérivé automatiquement de `SECRET_KEY`, ou peut être fixé via `TELEGRAM_WEBHOOK_SECRET`.
- **Protection des données** : Tous vos identifiants, tokens OAuth Google et conversations restent stockés localement sur votre machine et sont exclus du suivi Git via `.gitignore`.

---

## 📜 Licence

Ce projet est sous licence libre **MIT**. Consultez le fichier [LICENSE](LICENSE) pour plus de détails.
