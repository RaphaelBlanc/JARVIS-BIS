#!/usr/bin/env bash
# ==============================================================================
# deploy.sh — Script de gestion et déploiement de l'Assistant Personnel Unifié
# ==============================================================================
set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_RUNNER_DIR="$ROOT_DIR/home-runner"
RENDER_GATEWAY_DIR="$ROOT_DIR/render-gateway"
ACTION="${1:-install}"

case "$ACTION" in
    setup|wizard|config)
        python3 "$ROOT_DIR/setup_wizard.py"
        exit 0
        ;;

    agent)
        echo -e "${BLUE}Démarrage de l'agent local PC-Agent...${NC}"
        python3 "$ROOT_DIR/pc-agent/agent.py"
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
        echo -e "${BLUE}Démarrage des services...${NC}"
        # Détecte le mode depuis .env
        DEPLOY_MODE="local"
        if [ -f "$ROOT_DIR/.env" ]; then
            DEPLOY_MODE="$(grep '^DEPLOY_MODE=' "$ROOT_DIR/.env" | cut -d'=' -f2- | tr -d '"'\'' ' || echo 'local')"
        fi

        if [ "$DEPLOY_MODE" = "pc-agent" ]; then
            echo -e "${GREEN}Lancement en mode PC-Agent...${NC}"
            exec python3 "$ROOT_DIR/pc-agent/agent.py"
        fi

        echo -e "${GREEN}Lancement du Runner (8080) et du Gateway (8000)...${NC}"
        "$HOME_RUNNER_DIR/.venv/bin/uvicorn" main:app --app-dir "$HOME_RUNNER_DIR" --port 8080 &
        RUNNER_PID=$!
        "$RENDER_GATEWAY_DIR/.venv/bin/uvicorn" main:app --app-dir "$RENDER_GATEWAY_DIR" --port 8000 &
        GATEWAY_PID=$!

        trap "kill $RUNNER_PID $GATEWAY_PID 2>/dev/null || true" EXIT INT TERM
        wait
        exit 0
        ;;
esac

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}   🚀 Déploiement de l'Assistant Personnel Unifié     ${NC}"
echo -e "${BLUE}   (Architecture Hybride : Local & Serveur Distant)   ${NC}"
echo -e "${BLUE}======================================================${NC}"

# 1. Vérification des prérequis
echo -e "\n${YELLOW}[1/4] Vérification des prérequis...${NC}"

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python 3 est introuvable. Veuillez l'installer avant de continuer.${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Python 3 détecté : $(python3 --version)${NC}"

AGY_BIN="$(command -v agy || true)"
if [ -z "$AGY_BIN" ] && [ -f "$HOME/.gemini/antigravity-cli/agy" ]; then
    AGY_BIN="$HOME/.gemini/antigravity-cli/agy"
fi

if [ -n "$AGY_BIN" ]; then
    echo -e "${GREEN}✓ Antigravity CLI détecté : $AGY_BIN${NC}"
else
    echo -e "${YELLOW}⚠️ agy CLI non détecté dans le PATH. Assurez-vous qu'Antigravity CLI est bien installé.${NC}"
fi

# 2. Préparation des dépendances
echo -e "\n${YELLOW}[2/4] Préparation des environnements virtuels Python...${NC}"

# home-runner
if [ ! -d "$HOME_RUNNER_DIR/.venv" ]; then
    echo -e "Création du venv pour home-runner..."
    python3 -m venv "$HOME_RUNNER_DIR/.venv"
fi
"$HOME_RUNNER_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$HOME_RUNNER_DIR/.venv/bin/pip" install --quiet -r "$HOME_RUNNER_DIR/requirements.txt"
echo -e "${GREEN}✓ Dépendances home-runner installées.${NC}"

# render-gateway
if [ ! -d "$RENDER_GATEWAY_DIR/.venv" ]; then
    echo -e "Création du venv pour render-gateway..."
    python3 -m venv "$RENDER_GATEWAY_DIR/.venv"
fi
"$RENDER_GATEWAY_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$RENDER_GATEWAY_DIR/.venv/bin/pip" install --quiet -r "$RENDER_GATEWAY_DIR/requirements.txt"
"$RENDER_GATEWAY_DIR/.venv/bin/pip" install --quiet -r "$ROOT_DIR/requirements.txt" || true
echo -e "${GREEN}✓ Dépendances render-gateway installées.${NC}"

# 3. Configuration (.env)
echo -e "\n${YELLOW}[3/4] Vérification de la configuration (.env)...${NC}"

if [ ! -f "$ROOT_DIR/.env" ]; then
    echo -e "${YELLOW}Aucun fichier .env global trouvé. Lancement de l'installateur interactif...${NC}"
    python3 "$ROOT_DIR/setup_wizard.py"
else
    echo -e "${GREEN}✓ Fichier .env présent à la racine.${NC}"
fi

# Synchronise le secret et les configs vers home-runner et render-gateway si nécessaire
if [ -f "$ROOT_DIR/.env" ]; then
    cp "$ROOT_DIR/.env" "$HOME_RUNNER_DIR/.env" 2>/dev/null || true
    cp "$ROOT_DIR/.env" "$RENDER_GATEWAY_DIR/.env" 2>/dev/null || true
fi

# 4. Instructions de démarrage
echo -e "\n${YELLOW}[4/4] Prêt à l'emploi !${NC}"
echo -e "${GREEN}======================================================${NC}"
echo -e "${GREEN}   ✅ Déploiement préparé avec succès !               ${NC}"
echo -e "${GREEN}======================================================${NC}"
echo -e "\nCommandes disponibles :"
echo -e "  • ${BLUE}./deploy.sh setup${NC}    : Relancer l'assistant interactif de configuration des modules"
echo -e "  • ${BLUE}./deploy.sh start${NC}    : Lancer l'assistant (Runner + Gateway)"
echo -e "  • ${BLUE}./deploy.sh agent${NC}    : Lancer le PC-Agent (si ce PC est client d'un serveur distant)"
echo -e "  • ${BLUE}./deploy.sh status${NC}   : Vérifier la santé des services"
echo -e "\nPour configurer en service d'arrière-plan (systemd utilisateur) :"
echo -e "  mkdir -p ~/.config/systemd/user/"
echo -e "  cp $ROOT_DIR/systemd/*.service ~/.config/systemd/user/"
echo -e "  systemctl --user daemon-reload"
echo -e "  systemctl --user enable --now assistant-runner assistant-gateway"
echo -e "\nGuide API pour Noctalia : ${BLUE}$ROOT_DIR/docs/NOCTALIA_API.md${NC}"
echo -e "======================================================\n"
