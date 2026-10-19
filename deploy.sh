#!/usr/bin/env bash
# ==============================================================================
# deploy.sh — Script d'installation et de déploiement automatique de l'Assistant
# ==============================================================================
set -e

# Couleurs
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_RUNNER_DIR="$ROOT_DIR/home-runner"
RENDER_GATEWAY_DIR="$ROOT_DIR/render-gateway"

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}   🚀 Déploiement de l'Assistant Personnel Antigravity   ${NC}"
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

# 2. Configuration des environnements virtuels
echo -e "\n${YELLOW}[2/4] Préparation des environnements virtuels Python...${NC}"

# home-runner
if [ ! -d "$HOME_RUNNER_DIR/.venv" ]; then
    echo -e "Création du venv pour home-runner..."
    python3 -m venv "$HOME_RUNNER_DIR/.venv"
fi
echo -e "Installation des dépendances home-runner..."
"$HOME_RUNNER_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$HOME_RUNNER_DIR/.venv/bin/pip" install --quiet -r "$HOME_RUNNER_DIR/requirements.txt"
echo -e "${GREEN}✓ Dépendances home-runner installées.${NC}"

# render-gateway
if [ ! -d "$RENDER_GATEWAY_DIR/.venv" ]; then
    echo -e "Création du venv pour render-gateway..."
    python3 -m venv "$RENDER_GATEWAY_DIR/.venv"
fi
echo -e "Installation des dépendances render-gateway..."
"$RENDER_GATEWAY_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$RENDER_GATEWAY_DIR/.venv/bin/pip" install --quiet -r "$RENDER_GATEWAY_DIR/requirements.txt"
echo -e "${GREEN}✓ Dépendances render-gateway installées.${NC}"

# 3. Fichiers de configuration (.env)
echo -e "\n${YELLOW}[3/4] Vérification de la configuration (.env)...${NC}"

# Génération clé secrète partagée si besoin
GENERATED_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

if [ ! -f "$HOME_RUNNER_DIR/.env" ]; then
    echo -e "Génération de home-runner/.env..."
    cp "$HOME_RUNNER_DIR/.env.example" "$HOME_RUNNER_DIR/.env"
    sed -i "s/change_me_generate_a_strong_key/$GENERATED_SECRET/" "$HOME_RUNNER_DIR/.env"
    echo -e "${GREEN}✓ home-runner/.env créé avec une clé secrète aléatoire.${NC}"
else
    echo -e "${GREEN}✓ home-runner/.env existe déjà.${NC}"
fi

if [ ! -f "$RENDER_GATEWAY_DIR/.env" ]; then
    echo -e "Génération de render-gateway/.env..."
    cp "$RENDER_GATEWAY_DIR/.env.example" "$RENDER_GATEWAY_DIR/.env"
    # Utiliser la même clé secrète
    RUNNER_SECRET="$(grep '^SECRET_KEY=' "$HOME_RUNNER_DIR/.env" | cut -d'=' -f2-)"
    if [ -n "$RUNNER_SECRET" ]; then
        sed -i "s/change_me_generate_a_strong_key/$RUNNER_SECRET/" "$RENDER_GATEWAY_DIR/.env"
    fi
    # Par défaut pour serveur autonome : localhost:8080
    sed -i "s|https://ton-tunnel.trycloudflare.com|http://127.0.0.1:8080|" "$RENDER_GATEWAY_DIR/.env"
    echo -e "${GREEN}✓ render-gateway/.env créé (configuré par défaut sur http://127.0.0.1:8080).${NC}"
    echo -e "${YELLOW}👉 Pensez à éditer render-gateway/.env pour ajouter vos tokens DISCORD_TOKEN et TELEGRAM_TOKEN.${NC}"
    echo -e "${YELLOW}👉 Sécurité fail-closed : renseignez DISCORD_ALLOWED_USER_IDS et TELEGRAM_ALLOWED_USER_IDS, sinon définissez ALLOW_ALL_USERS=1 pour ouvrir l'accès.${NC}"
else
    echo -e "${GREEN}✓ render-gateway/.env existe déjà.${NC}"
fi

# 4. Instructions de démarrage
echo -e "\n${YELLOW}[4/4] Prêt à l'emploi !${NC}"
echo -e "${GREEN}======================================================${NC}"
echo -e "${GREEN}   ✅ Déploiement préparé avec succès !               ${NC}"
echo -e "${GREEN}======================================================${NC}"
echo -e "\nPour tester immédiatement :"
echo -e "  Terminal 1 : ${BLUE}$HOME_RUNNER_DIR/.venv/bin/uvicorn main:app --app-dir $HOME_RUNNER_DIR --port 8080${NC}"
echo -e "  Terminal 2 : ${BLUE}$RENDER_GATEWAY_DIR/.venv/bin/uvicorn main:app --app-dir $RENDER_GATEWAY_DIR --port 8000${NC}"
echo -e "\nPour configurer en service d'arrière-plan (systemd utilisateur) :"
echo -e "  mkdir -p ~/.config/systemd/user/"
echo -e "  cp $ROOT_DIR/systemd/*.service ~/.config/systemd/user/"
echo -e "  systemctl --user daemon-reload"
echo -e "  systemctl --user enable --now assistant-runner assistant-gateway"
echo -e "  systemctl --user status assistant-runner assistant-gateway"
echo -e "\nPour utiliser l'assistant depuis un autre réseau (Noctalia, téléphone) :"
echo -e "  ${BLUE}$ROOT_DIR/scripts/expose-gateway.sh quick${NC}   # test Cloudflare, URL temporaire"
echo -e "  ${BLUE}$ROOT_DIR/scripts/expose-gateway.sh cloudflare --hostname assistant.tondomaine.fr${NC}"
echo -e "  Guide : ${BLUE}$ROOT_DIR/docs/ACCES_DISTANT.md${NC}"
echo -e "======================================================\n"
