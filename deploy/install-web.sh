#!/usr/bin/env bash
# Install and start Staff HQ as a systemd user service. Run from the repo root: ./deploy/install-web.sh
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
uv="$(command -v uv)"
log="${GIEOK_ROOT:-$HOME/.local/share/gieok}/web.log"
mkdir -p "$(dirname "$log")" "$HOME/.config/systemd/user"
sed -e "s|@ROOT@|$root|g" -e "s|@UV@|$uv|g" -e "s|@PATH@|$PATH|g" -e "s|@LOG@|$log|g" \
  "$root/deploy/gieok-web.service" > "$HOME/.config/systemd/user/gieok-web.service"
systemctl --user daemon-reload
systemctl --user enable --now gieok-web.service
systemctl --user --no-pager status gieok-web.service | head -5
