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

### Catalogue AGY et sélection Neon / Hydrogen / Argon

Le runner natif (`main:app`) lit le catalogue du compte avec `agy models`, dans
un thread en arrière-plan : les routes HTTP n'attendent pas cette commande.
Le CLI doit annoncer la sous-commande `models` dans son aide et être connecté
sous le compte qui exécute le service. La sortie attendue est une ligne
`identifiant<TAB>nom` par modèle ; aucune famille inaccessible au compte n'est
ajoutée artificiellement.

Une relecture démarre en arrière-plan à chaque lancement, même si le cache est
récent : un modèle nouvellement publié comme Neon est ainsi découvert après
redémarrage, tandis que l'ancienne liste répond immédiatement. Ensuite, la
lecture démarre à la demande via `/models` ou
`/nc/engines` quand la liste a plus de six heures. Après un échec, le runner
attend quinze minutes avant de réessayer et conserve la dernière bonne liste,
ou la liste historique de repli si aucune lecture n'a réussi. Le cache privé
`home-runner/.nc-jarvis-agy-models.json` utilise le même format que l'adaptateur
Noctalia. Après une nouvelle connexion AGY, supprimer ce cache puis redémarrer
le runner permet de relire immédiatement le compte.

`/nc/engines` annonce `catalogue_compte`, `catalogue_en_cours`, les identifiants
réels et l'effort `xhigh` quand une variante `-xhigh` existe. L'île peut ainsi
activer Neon, Hydrogen et Argon et relire le catalogue à la fin du chargement.
L'alignement modèle/effort vaut pour les réglages, leur restauration et `/run` :

- Hydrogen avec `xhigh` ou `max` choisit `gemini-4-hydrogen-xhigh` si annoncé,
  avec l'effort `xhigh` ; sans cette variante, le runner utilise `-high` si disponible.
- Un modèle `-xhigh` donné sans effort prend `xhigh`.
- Les variantes Neon `gemini-4-neon-low`, `-medium`, `-high` et `-xhigh` sont
  reprises du catalogue du compte. `max` sélectionne `gemini-4-neon-xhigh`
  lorsqu'il existe ; `gemini-pro-orchestrator-neon` reste un autre identifiant,
  sans substitution vers ce modèle.
- Une variante absente n'est pas inventée : le modèle conserve son suffixe et
  l'effort est aligné dessus. Opus Thinking continue d'ignorer l'effort.

Les protections et routes de l'overlay Noctalia restent nécessaires à son
déploiement : cette correction du moteur natif ne remplace pas l'overlay.
Le mainteneur de noctalia-config doit examiner et ajouter la révision du
correctif à `VALIDATED_REVISIONS` avant de la déployer avec son outil.

Tests sans compte ni réseau, depuis la racine du dépôt :

```sh
PYTHONPATH=home-runner python3 -B -m unittest discover -s home-runner/tests -p test_agy_catalogue.py -v
```

Ces tests vérifient le catalogue, le cache, la sélection et les arguments du
vrai bridge avec un faux exécutable. Ils ne prouvent pas qu'un compte réel
accepte Hydrogen/xhigh : après déploiement et redémarrage, appliquer ce choix,
vérifier `/session`, puis envoyer un message réel reste nécessaire.

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
