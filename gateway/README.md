# 🌐 Gateway — Passerelle API & Bots (FastAPI)

Composant **Gateway** de l'assistant personnel JARVIS.
Serveur FastAPI centralisant les points d'entrée :
- **Bots de messagerie** : Telegram (polling ou webhook) & Discord (commandes slash).
- **API REST & SSE** : pour l'intégration du bureau Linux Noctalia (`nc-assistant`, widget barre, île dynamique Quickshell, Spotlight `Super+R`).
- **Planificateur de tâches** : briefing matinal automatique à 08h00 et rappels persistants (SQLite).
- **Modules MCP / Outils** : exécution directe de Moodle, Finances, Courses, Obsidian, Pomodoro, Pilotage PC...

---

## 🚀 Démarrage

```bash
# Démarrage direct via uvicorn
cd gateway
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```
Ou via le script racine :
```bash
./deploy.sh start
```

---

## ⚙️ Variables d'environnement principales

Toutes les variables sont configurées dans le fichier `.env` à la racine du projet (ou générées interactivement via `./deploy.sh setup`).

| Variable | Requis | Description |
|---|---|---|
| `SECRET_KEY` | ✅ | Clé secrète d'authentification API (`X-Secret-Key`) |
| `PORT` | ⚠️ optionnel | Port d'écoute HTTP (défaut : `8000`) |
| `TELEGRAM_TOKEN` | ⚠️ optionnel | Token du bot Telegram (via @BotFather) |
| `DISCORD_TOKEN` | ⚠️ optionnel | Token du bot Discord |
| `TELEGRAM_ALLOWED_USER_IDS` | ⚠️ optionnel | IDs Telegram autorisés (séparés par virgules) |
| `DISCORD_ALLOWED_USER_IDS` | ⚠️ optionnel | IDs Discord autorisés (séparés par virgules) |
| `GROQ_API_KEY` | ⚠️ optionnel | Clé API pour transcription vocale ultra-rapide (Whisper) |
