#!/usr/bin/env bash
# ==============================================================================
# deploy.sh — Script d'installation et de lancement de l'Assistant Personnel
# ==============================================================================
set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_RUNNER_DIR="$ROOT_DIR/home-runner"
GATEWAY_DIR="$ROOT_DIR/gateway"
ACTION="${1:-install}"

case "$ACTION" in
    setup|wizard|config)
        python3 "$ROOT_DIR/setup_wizard.py"
        exit 0
        ;;

    status)
        echo -e "${BLUE}Vérification de l'état des services...${NC}"
        curl -s http://127.0.0.1:8000/ping || echo -e "${RED}Gateway (8000) non joignable${NC}"
        echo ""
        curl -s http://127.0.0.1:8080/ping || echo -e "${RED}Runner (8080) non joignable${NC}"
        echo ""
        exit 0
        ;;

    start)
        echo -e "${BLUE}Démarrage de l'Assistant Personnel...${NC}"
        "$HOME_RUNNER_DIR/.venv/bin/uvicorn" main:app --app-dir "$HOME_RUNNER_DIR" --port 8080 &
        RUNNER_PID=$!
        "$GATEWAY_DIR/.venv/bin/uvicorn" main:app --app-dir "$GATEWAY_DIR" --port 8000 &
        GATEWAY_PID=$!

        trap "kill $RUNNER_PID $GATEWAY_PID 2>/dev/null || true" EXIT INT TERM
        wait
        exit 0
        ;;
esac

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}   🚀 Installation de l'Assistant Personnel           ${NC}"
echo -e "${BLUE}======================================================${NC}"

# 1. Vérification Python
echo -e "\n${YELLOW}[1/3] Vérification des prérequis...${NC}"
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python 3 est introuvable. Veuillez l'installer avant de continuer.${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Python 3 détecté : $(python3 --version)${NC}"

# 2. Environnements virtuels
echo -e "\n${YELLOW}[2/3] Préparation des environnements virtuels Python...${NC}"

if [ ! -d "$HOME_RUNNER_DIR/.venv" ]; then
    echo -e "Création du venv pour home-runner..."
    python3 -m venv "$HOME_RUNNER_DIR/.venv"
fi
"$HOME_RUNNER_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$HOME_RUNNER_DIR/.venv/bin/pip" install --quiet -r "$HOME_RUNNER_DIR/requirements.txt"

if [ ! -d "$GATEWAY_DIR/.venv" ]; then
    echo -e "Création du venv pour gateway..."
    python3 -m venv "$GATEWAY_DIR/.venv"
fi
"$GATEWAY_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$GATEWAY_DIR/.venv/bin/pip" install --quiet -r "$GATEWAY_DIR/requirements.txt"
"$GATEWAY_DIR/.venv/bin/pip" install --quiet -r "$ROOT_DIR/requirements.txt" || true
echo -e "${GREEN}✓ Dépendances installées.${NC}"

# 3. Configuration (.env)
echo -e "\n${YELLOW}[3/3] Configuration (.env)...${NC}"
if [ ! -f "$ROOT_DIR/.env" ]; then
    python3 "$ROOT_DIR/setup_wizard.py"
else
    echo -e "${GREEN}✓ Fichier .env présent.${NC}"
fi

if [ -f "$ROOT_DIR/.env" ]; then
    cp "$ROOT_DIR/.env" "$HOME_RUNNER_DIR/.env" 2>/dev/null || true
    cp "$ROOT_DIR/.env" "$GATEWAY_DIR/.env" 2>/dev/null || true
fi

echo -e "\n${GREEN}======================================================${NC}"
echo -e "${GREEN}   ✅ Prêt à l'emploi !                               ${NC}"
echo -e "${GREEN}======================================================${NC}"
echo -e "Commandes :"
echo -e "  • ${BLUE}./deploy.sh setup${NC}  : Configurer les tokens et outils MCP"
echo -e "  • ${BLUE}./deploy.sh start${NC}  : Lancer l'assistant"
echo -e "  • ${BLUE}./deploy.sh status${NC} : Vérifier la santé"
echo -e "======================================================\n"
