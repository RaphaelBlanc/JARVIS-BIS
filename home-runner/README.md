# Home Runner

Composant local de l'assistant personnel. Ce serveur **FastAPI** tourne sur le PC Linux et sert de passerelle entre le serveur Render (`render-gateway`) et **Antigravity CLI** (`agy`).

```
[Render / render-gateway]
        │  POST /run  (HTTPS + X-Secret-Key)
        ▼
[home-runner  :8080]  ←── ce composant
        │  asyncio.create_subprocess_exec
        ▼
[agy CLI]  ──► réponse LLM
```

---

## Prérequis

- Python ≥ 3.11
- `agy` installé et accessible (`which agy` doit retourner un chemin)
- Un port ouvert/transféré vers Internet (ou tunnel ngrok/cloudflared) sur le port configuré

---

## Installation

```bash
git clone <ce dépôt>
cd home-runner
bash setup.sh
```

Le script :
1. Crée un venv Python dans `.venv/`
2. Installe les dépendances (`requirements.txt`)
3. Copie `.env.example` → `.env`

---

## Configuration (`.env`)

Édite `.env` après l'installation :

```dotenv
# Clé secrète partagée avec render-gateway
SECRET_KEY=<générer avec: python3 -c "import secrets; print(secrets.token_hex(32))">

# Port d'écoute
PORT=8080

# Timeout agy en secondes
AGY_TIMEOUT=120
```

> **Important** : `SECRET_KEY` est **obligatoire**. Le serveur refuse de démarrer sans elle.

---

## Lancement manuel

```bash
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8080
```

---

## Service systemd (démarrage automatique)

```bash
# Copier l'unit
sudo cp assistant.service /etc/systemd/system/

# Recharger systemd et activer
sudo systemctl daemon-reload
sudo systemctl enable assistant-home-runner
sudo systemctl start assistant-home-runner

# Vérifier le statut
sudo systemctl status assistant-home-runner

# Consulter les logs
journalctl -u assistant-home-runner -f
```

---

## Endpoints de l'API

| Méthode | Chemin | Auth | Description |
|---|---|---|---|
| `GET` | `/ping` | ✗ | Vérification de vie rapide (`{"status":"ok"}`) |
| `GET` | `/health` | ✗ | État de santé détaillé et disponibilité d'`agy` |
| `POST` | `/run` | ✓ | Exécute un prompt via Antigravity CLI |
| `GET` | `/models` | ✓ | Liste les modèles supportés et le modèle actif |
| `GET` | `/session` | ✓ | Réglages de la session active (modèle, effort, conversation) |
| `POST` | `/session` | ✓ | Met à jour le modèle, l'effort ou le mode d'exécution |
| `POST` | `/session/reset` | ✓ | Réinitialise la session conversationnelle |
| `GET` | `/conversations` | ✓ | Liste les conversations passées récentes |
| `POST` | `/conversation/resume` | ✓ | Reprend une conversation passée par ID |
| `GET` | `/conversation/{id}/history` | ✓ | Historique des échanges d'une conversation |
| `DELETE` | `/conversation/{id}` | ✓ | Supprime une conversation passée (ou `all`) |
| `POST` | `/conversation/delete` | ✓ | Supprime une conversation via payload JSON |

### Authentification

Toutes les requêtes à `/run` doivent inclure le header :

```
X-Secret-Key: <votre SECRET_KEY>
```

---

## Tests avec curl

```bash
# Ping
curl http://localhost:8080/ping

# Health check
curl http://localhost:8080/health

# Envoyer un prompt à agy
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -H "X-Secret-Key: <votre_clé>" \
  -d '{"prompt": "Quelle est la capitale de la France ?", "timeout": 60}'
```

### Exemple de réponse `/run`

```json
{
  "success": true,
  "output": "La capitale de la France est Paris.",
  "error": null
}
```

---

## Structure des fichiers

```
home-runner/
├── main.py            # Serveur FastAPI (routes, auth, logging)
├── agy_bridge.py      # Subprocess async vers agy + nettoyage ANSI
├── requirements.txt   # Dépendances Python
├── .env.example       # Modèle de configuration
├── .env               # Configuration locale (ne pas committer !)
├── assistant.service  # Unit systemd
├── setup.sh           # Script d'installation
└── README.md          # Cette documentation
```

---

## Sécurité

- Le serveur **n'expose pas Swagger UI** (API privée).
- `SECRET_KEY` doit être une chaîne aléatoire forte (≥ 32 octets hex recommandé).
- N'exposez jamais `.env` dans un dépôt public.
- Restreignez l'accès réseau au seul IP Render si possible (pare-feu / ufw).
