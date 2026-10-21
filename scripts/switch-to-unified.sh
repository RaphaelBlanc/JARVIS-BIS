#!/usr/bin/env bash
# switch-to-unified.sh — Bascule les services systemd vers ~/assistant-unifie
# À exécuter avec sudo sur le serveur : sudo ./scripts/switch-to-unified.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="/home/pi/assistant-backups/systemd-$(date +%Y%m%d-%H%M%S)"

if [ "$(id -u)" -ne 0 ]; then
    echo "❌ Ce script doit être lancé avec sudo." >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
for unit in assistant-runner assistant-gateway; do
    if [ -f "/etc/systemd/system/$unit.service" ]; then
        cp -a "/etc/systemd/system/$unit.service" "$BACKUP_DIR/"
    fi
    install -m 644 "$ROOT_DIR/systemd/$unit.service" "/etc/systemd/system/$unit.service"
done

systemctl daemon-reload
systemctl stop assistant-gateway assistant-runner 2>/dev/null || true
systemctl enable assistant-runner assistant-gateway

systemctl restart assistant-runner
sleep 3
systemctl restart assistant-gateway
sleep 3

echo "✅ Services basculés vers $ROOT_DIR"
echo "   Sauvegarde des anciennes unités : $BACKUP_DIR"
systemctl --no-pager --lines=0 status assistant-runner assistant-gateway || true
