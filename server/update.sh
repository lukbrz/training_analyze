#!/usr/bin/env bash
# Update the server: pull the code, rebuild the image, restart every athlete's service.
# Run as the service user from the repository root (or anywhere inside it).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git pull --ff-only
podman build -t training-analyze -f server/Containerfile .
for unit in $(systemctl --user list-units --type=service --all --plain --no-legend 'training-analyze-*' | awk '{print $1}'); do
    echo "Restarting $unit"
    systemctl --user restart "$unit"
done
podman image prune -f >/dev/null
echo "Done. Activities are reprocessed with the new code at the next nightly run."
