#!/usr/bin/env bash
# ==============================================================================
# server-control.sh — actions serveur whitelistées pour le bot Discord
#
#   server-control.sh status
#   server-control.sh restart-runner
#   server-control.sh restart-gateway
#   server-control.sh tunnel-status
#   server-control.sh logs-gateway
#   server-control.sh logs-runner
#
# Aucune commande utilisateur n'est exécutée : seules les actions ci-dessus
# existent. Les actions privilégiées utilisent `sudo -n` (sans mot de passe) ;
# si sudo n'est pas configuré, le script explique quoi faire.
# ==============================================================================
set -euo pipefail

GATEWAY_PORT="${GATEWAY_PORT:-8000}"
RUNNER_PORT="${RUNNER_PORT:-8080}"
SLEEP_AFTER_RESTART="${SERVER_CONTROL_SLEEP:-3}"
ACTION="${1:-status}"

info() { printf '→ %s\n' "$*"; }
ok() { printf '✓ %s\n' "$*"; }
err() { printf '✗ %s\n' "$*" >&2; }

service_state() {
  systemctl is-active "$1" 2>/dev/null || true
}

user_service_state() {
  systemctl --user is-active "$1" 2>/dev/null || true
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

require_privileges() {
  if [ "$(id -u)" -ne 0 ] && ! sudo -n true 2>/dev/null; then
    err "sudo sans mot de passe n'est pas configuré pour $(whoami)."
    err "Ajoute une règle sudoers limitée à server-control.sh, ou lance l'action en SSH."
    exit 1
  fi
}

health() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 3 "$url" 2>/dev/null || echo "indisponible"
  else
    echo "curl absent"
  fi
}

act_status() {
  echo "Gateway (assistant-gateway) : $(service_state assistant-gateway)"
  echo "Runner  (assistant-runner)  : $(service_state assistant-runner)"
  if [ "$(id -u)" -ne 0 ]; then
    echo "Tunnel nommé                : $(user_service_state assistant-tunnel)"
    echo "Tunnel temporaire           : $(user_service_state assistant-tunnel-quick)"
  fi
  echo "Gateway /health             : $(health "http://127.0.0.1:${GATEWAY_PORT}/health")"
  echo "Runner  /health             : $(health "http://127.0.0.1:${RUNNER_PORT}/health")"
}

act_restart_runner() {
  require_privileges
  info "Redémarrage de assistant-runner…"
  if ! as_root systemctl restart assistant-runner; then
    err "Échec du redémarrage de assistant-runner."
    exit 1
  fi
  sleep "$SLEEP_AFTER_RESTART"
  ok "Runner : $(service_state assistant-runner)"
  echo "Runner /health : $(health "http://127.0.0.1:${RUNNER_PORT}/health")"
}

act_restart_gateway() {
  require_privileges
  if ! command -v systemd-run >/dev/null 2>&1; then
    err "systemd-run est requis pour redémarrer le gateway sans couper sa propre réponse."
    exit 1
  fi
  local unit="assistant-gateway-restart-$(date +%s)"
  info "Redémarrage du gateway programmé dans ${SLEEP_AFTER_RESTART} s…"
  as_root systemd-run --collect --on-active="$SLEEP_AFTER_RESTART" \
    --timer-property=AccuracySec=1s --unit="$unit" \
    /bin/systemctl restart assistant-gateway >/dev/null
  ok "Redémarrage programmé (unité ${unit}). Le bot va se déconnecter puis revenir."
}

act_tunnel_status() {
  if [ "$(id -u)" -eq 0 ]; then
    echo "Tunnel : exécute cette action en tant que $(logname 2>/dev/null || echo pi) pour voir les services utilisateur."
    return 0
  fi
  echo "Tunnel nommé      : $(user_service_state assistant-tunnel)"
  echo "Tunnel temporaire : $(user_service_state assistant-tunnel-quick)"
  if systemctl --user is-active --quiet assistant-tunnel-quick 2>/dev/null; then
    local url
    url="$(journalctl --user -u assistant-tunnel-quick -n 100 --no-pager 2>/dev/null |
      grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -n 1 || true)"
    echo "URL temporaire    : ${url:-inconnue}"
  fi
  if command -v cloudflared >/dev/null 2>&1; then
    echo
    echo "Tunnels Cloudflare :"
    cloudflared tunnel list 2>/dev/null || echo "liste indisponible"
  fi
}

act_logs_gateway() {
  require_privileges
  as_root journalctl -u assistant-gateway -n 40 --no-pager
}

act_logs_runner() {
  require_privileges
  as_root journalctl -u assistant-runner -n 40 --no-pager
}

case "$ACTION" in
  status) act_status ;;
  restart-runner) act_restart_runner ;;
  restart-gateway) act_restart_gateway ;;
  tunnel-status) act_tunnel_status ;;
  logs-gateway) act_logs_gateway ;;
  logs-runner) act_logs_runner ;;
  -h|--help|help)
    sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    err "Action inconnue : ${ACTION}"
    err "Actions : status, restart-runner, restart-gateway, tunnel-status, logs-gateway, logs-runner"
    exit 2
    ;;
esac
