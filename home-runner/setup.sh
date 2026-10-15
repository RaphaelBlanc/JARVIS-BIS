#!/bin/bash
# setup.sh — Installation du composant home-runner
# Usage : bash setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Installation du home-runner ==="
echo "Répertoire : $SCRIPT_DIR"
echo ""

# 1. Créer le venv Python
echo "→ Création du venv Python (.venv)…"
python3 -m venv .venv
echo "✅ Venv créé."

# 2. Installer les dépendances
echo "→ Installation des dépendances Python…"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "✅ Dépendances installées."

# 3. Copier .env.example → .env si .env n'existe pas encore
if [ ! -f .env ]; then
    cp .env.example .env
    echo "✅ Fichier .env créé depuis .env.example."
else
    echo "ℹ️  Fichier .env déjà présent — non écrasé."
fi

echo ""
echo "============================================"
echo "  Installation terminée !"
echo "============================================"
echo ""
echo "👉 Étapes suivantes :"
echo ""
echo "  1. Édite .env et remplace SECRET_KEY par une vraie clé :"
echo "     python3 -c \"import secrets; print(secrets.token_hex(32))\""
echo ""
echo "  2. Lance le serveur manuellement pour tester :"
echo "     .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8080"
echo ""
echo "  3. (Optionnel) Installe le service systemd pour le démarrage automatique :"
echo "     sudo cp assistant.service /etc/systemd/system/"
echo "     sudo systemctl daemon-reload"
echo "     sudo systemctl enable assistant-home-runner"
echo "     sudo systemctl start assistant-home-runner"
echo ""
echo "  4. Teste avec curl :"
echo "     curl http://localhost:8080/ping"
echo "     curl -X POST http://localhost:8080/run \\"
echo "       -H 'Content-Type: application/json' \\"
echo "       -H 'X-Secret-Key: <ta_clé>' \\"
echo "       -d '{\"prompt\": \"Quelle heure est-il ?\"}'"
echo ""
