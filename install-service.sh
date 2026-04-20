#!/usr/bin/env bash
set -euo pipefail

SERVICE=gemma-rpi-agent
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

cp "$REPO_DIR/$SERVICE.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
systemctl status "$SERVICE" --no-pager
