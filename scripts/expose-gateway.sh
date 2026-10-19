#!/usr/bin/env bash
# ==============================================================================
# expose-gateway.sh — rendre le gateway accessible depuis un autre réseau
#
# Le client Noctalia (nc-assistant) doit joindre le GATEWAY (port 8000), jamais
# le home-runner (port 8080), qui est une API privée.
#
#   scripts/expose-gateway.sh quick
#       Tunnel Cloudflare temporaire, sans compte. URL aléatoire, change au
#       redémarrage. Reste au premier plan (Ctrl+C pour arrêter).
#
#   scripts/expose-gateway.sh quick-service
#       Même tunnel temporaire, mais en service systemd utilisateur : la
#       commande rend la main et affiche l'URL. Recommandé via Discord/SSH.
#
#   scripts/expose-gateway.sh cloudflare --hostname assistant.example.com
#       Tunnel Cloudflare nommé + DNS + service systemd utilisateur (durable).
#       Prérequis : cloudflared installé et `cloudflared tunnel login` fait.
#
#   scripts/expose-gateway.sh tailscale
#       Tailscale Serve sur le port 8000 (réseau privé, aucun port ouvert).
#       Prérequis : Tailscale installé et connecté sur le serveur ET le PC.
#
#   scripts/expose-gateway.sh status | stop
# ==============================================================================
set -euo pipefail

GATEWAY_PORT="${GATEWAY_PORT:-8000}"
LOCAL_URL="http://127.0.0.1:${GATEWAY_PORT}"
SERVICE="assistant-tunnel"
QUICK_SERVICE="assistant-tunnel-quick"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/${SERVICE}.service"
QUICK_UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/${QUICK_SERVICE}.service"

info() { printf '\033[1;34m→\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

need() {
  command -v "$1" >/dev/null 2>&1 || die "$1 est requis. Installe-le puis relance."
}

verifier_gateway_local() {
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 3 "${LOCAL_URL}/health" >/dev/null 2>&1; then
      ok "Gateway local joignable sur ${LOCAL_URL}."
    else
      warn "Le gateway ne répond pas sur ${LOCAL_URL}. Démarre assistant-gateway avant d'exposer."
    fi
  fi
}

cloudflare_quick() {
  need cloudflared
  verifier_gateway_local
  warn "Tunnel temporaire : l'URL change à chaque redémarrage."
  warn "Pour un usage durable, utilise : $0 cloudflare --hostname assistant.tondomaine.fr"
  info "Copie l'URL https://…trycloudflare.com affichée ci-dessous dans nc-assistant."
  exec cloudflared tunnel --no-autoupdate --url "${LOCAL_URL}"
}

cloudflare_quick_service() {
  need cloudflared
  need systemctl
  verifier_gateway_local

  local bin
  bin="$(command -v cloudflared)"
  mkdir -p "$(dirname "$QUICK_UNIT")"
  cat >"$QUICK_UNIT" <<EOF
[Unit]
Description=Assistant personnel — tunnel Cloudflare temporaire vers le gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${bin} --no-autoupdate --url ${LOCAL_URL}
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user disable --now "${QUICK_SERVICE}.service" >/dev/null 2>&1 || true
  systemctl --user reset-failed "${QUICK_SERVICE}.service" >/dev/null 2>&1 || true
  local since
  since="$(date -d '2 seconds ago' '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date '+%Y-%m-%d %H:%M:%S')"
  systemctl --user enable --now "${QUICK_SERVICE}.service" >/dev/null

  local url=""
  local i
  for i in $(seq 1 30); do
    url="$(journalctl --user -u "${QUICK_SERVICE}.service" --since "$since" -n 100 --no-pager 2>/dev/null |
      grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -n 1 || true)"
    [ -n "$url" ] && break
    sleep 1
  done

  if [ -z "$url" ]; then
    warn "URL trycloudflare.com introuvable après 30 s."
    systemctl --user status "${QUICK_SERVICE}.service" --no-pager 2>&1 | tail -n 20 || true
    die "Vérifie les journaux : journalctl --user -u ${QUICK_SERVICE}.service -n 80 --no-pager"
  fi

  ok "Tunnel temporaire actif. URL publique : ${url}"
  echo
  echo "Sur le PC Noctalia :"
  echo "  nc-assistant configurer"
  echo "    URL     : ${url}"
  echo "    USER_ID : $(whoami)"
  echo "  nc-assistant verifier"
  echo
  warn "URL temporaire : elle change si le service redémarre. Pour l'arrêter : $0 stop"
}

tunnel_id() {
  cloudflared tunnel list --output json 2>/dev/null |
    python3 -c 'import json,sys; name=sys.argv[1]; print(next((t["id"] for t in json.load(sys.stdin) if t.get("name")==name), ""))' "$1"
}

cloudflare_named() {
  local name="assistant" hostname=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --hostname) hostname="${2:-}"; shift 2 ;;
      --name) name="${2:-}"; shift 2 ;;
      *) die "Option inconnue : $1" ;;
    esac
  done
  [ -n "$hostname" ] || die "Utilise --hostname assistant.tondomaine.fr"

  need cloudflared
  need python3
  verifier_gateway_local

  if [ ! -f "${HOME}/.cloudflared/cert.pem" ]; then
    info "Connexion à Cloudflare (navigateur)…"
    cloudflared tunnel login
  fi

  local id
  id="$(tunnel_id "$name")"
  if [ -z "$id" ]; then
    info "Création du tunnel ${name}…"
    cloudflared tunnel create "$name" >/dev/null
    id="$(tunnel_id "$name")"
  fi
  [ -n "$id" ] || die "Impossible de déterminer l'ID du tunnel ${name}."

  local credentials="${HOME}/.cloudflared/${id}.json"
  [ -f "$credentials" ] || die "Fichier de credentials introuvable : ${credentials}"

  info "Configuration de l'ingress vers ${LOCAL_URL}…"
  cat >"${HOME}/.cloudflared/config.yml" <<EOF
# Généré par assistant-perso/scripts/expose-gateway.sh
tunnel: ${id}
credentials-file: ${credentials}

ingress:
  - hostname: ${hostname}
    service: ${LOCAL_URL}
  - service: http_status:404
EOF

  info "Association DNS ${hostname} → tunnel ${name}…"
  cloudflared tunnel route dns "$name" "$hostname" 2>/dev/null || \
    warn "Le DNS existait peut-être déjà ; vérifie ${hostname} dans Cloudflare."

  local bin
  bin="$(command -v cloudflared)"
  mkdir -p "$(dirname "$UNIT")"
  cat >"$UNIT" <<EOF
[Unit]
Description=Assistant personnel — tunnel Cloudflare vers le gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${bin} --no-autoupdate --config ${HOME}/.cloudflared/config.yml tunnel run
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now "${SERVICE}.service"
  ok "Tunnel actif. URL publique : https://${hostname}"
  echo
  echo "Sur le PC Noctalia :"
  echo "  nc-assistant configurer"
  echo "    URL     : https://${hostname}"
  echo "    USER_ID : $(whoami)"
  echo "  nc-assistant verifier"
}

tailscale_access() {
  need tailscale
  verifier_gateway_local

  if ! tailscale status >/dev/null 2>&1; then
    die "Tailscale n'est pas connecté. Lance : sudo tailscale up"
  fi

  info "Publication du gateway sur le tailnet…"
  if ! tailscale serve --bg "$GATEWAY_PORT" >/dev/null 2>&1; then
    tailscale serve --bg "${LOCAL_URL}" >/dev/null 2>&1 || \
      die "Tailscale Serve a échoué. Active HTTPS/MagicDNS dans la console Tailscale."
  fi

  local dns
  dns="$(tailscale status --json 2>/dev/null |
    python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
  [ -n "$dns" ] || die "Nom DNS Tailscale introuvable. Active MagicDNS."

  ok "Gateway accessible sur : https://${dns}"
  echo
  echo "Le PC Noctalia doit aussi être connecté au même tailnet."
  echo "  nc-assistant configurer"
  echo "    URL     : https://${dns}"
  echo "  nc-assistant verifier"
}

statut() {
  echo "Gateway local : ${LOCAL_URL}"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 3 "${LOCAL_URL}/health" || warn "Gateway local injoignable."
    echo
  fi
  echo
  echo "Service ${SERVICE} (tunnel nommé) :"
  systemctl --user is-active "${SERVICE}.service" 2>/dev/null || echo "inactif"
  echo "Service ${QUICK_SERVICE} (tunnel temporaire) :"
  systemctl --user is-active "${QUICK_SERVICE}.service" 2>/dev/null || echo "inactif"
  if systemctl --user is-active --quiet "${QUICK_SERVICE}.service" 2>/dev/null; then
    echo "URL temporaire :"
    journalctl --user -u "${QUICK_SERVICE}.service" -n 100 --no-pager 2>/dev/null |
      grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -n 1 || true
  fi
  if command -v tailscale >/dev/null 2>&1; then
    echo
    echo "Tailscale Serve :"
    tailscale serve status 2>/dev/null || echo "non configuré"
  fi
}

arreter() {
  systemctl --user disable --now "${SERVICE}.service" 2>/dev/null || true
  systemctl --user disable --now "${QUICK_SERVICE}.service" 2>/dev/null || true
  if command -v tailscale >/dev/null 2>&1; then
    tailscale serve reset >/dev/null 2>&1 || true
  fi
  ok "Exposition distante arrêtée."
}

case "${1:-}" in
  quick) cloudflare_quick ;;
  quick-service|quick-service-start) cloudflare_quick_service ;;
  cloudflare) shift; cloudflare_named "$@" ;;
  tailscale) tailscale_access ;;
  status) statut ;;
  stop) arreter ;;
  ""|-h|--help|help)
    sed -n '3,24p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *) die "Commande inconnue : $1 (quick, quick-service, cloudflare, tailscale, status, stop)" ;;
esac
