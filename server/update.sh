#!/usr/bin/env bash
# Update the server: pull the code, rebuild the image, restart every athlete's service, and
# check each one answers. If one doesn't, go back to the previous image. Run as the service
# user; enable-auto-update.sh runs it every night.
#   server/update.sh           update if the code changed since the image was built
#   server/update.sh --force   rebuild and restart anyway
set -euo pipefail
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
force="${1:-}"
image=localhost/training-analyze

git pull --ff-only --quiet
commit="$(git rev-parse --short HEAD)"
built="$(podman image inspect --format '{{index .Config.Labels "ta.commit"}}' "$image:latest" 2>/dev/null || true)"
if [ "$commit" = "$built" ] && [ "$force" != "--force" ]; then
    echo "Up to date ($commit); nothing to do."
    exit 0
fi
echo "Updating ${built:-(no image)} -> $commit: $(git log -1 --format=%s)"

# Keep the running image as :previous, to go back to if the new one doesn't start.
podman image exists "$image:latest" && podman tag "$image:latest" "$image:previous"
podman build --quiet --label "ta.commit=$commit" --build-arg "TA_VERSION=$commit" \
    -t "$image:latest" -f server/Containerfile . >/dev/null

# One unit per athlete, from their quadlet files (not this updater's own unit).
units=()
for file in "$HOME"/.config/containers/systemd/training-analyze-*.container; do
    [ -e "$file" ] && units+=("$(basename "$file" .container)")
done

restart_all() {
    for unit in "${units[@]}"; do
        systemctl --user restart "$unit"
    done
}

healthy() {  # the quadlet's HealthCmd, retried for up to two minutes
    for _ in $(seq 40); do
        podman healthcheck run "$1" >/dev/null 2>&1 && return 0
        sleep 3
    done
    return 1
}

restart_all
failed=()
for unit in "${units[@]}"; do
    healthy "$unit" || failed+=("$unit")
done

if [ ${#failed[@]} -gt 0 ]; then
    echo "Not answering after the update: ${failed[*]}. Last log lines:" >&2
    for unit in "${failed[@]}"; do
        journalctl --user -u "$unit" -n 20 --no-pager >&2 || true
    done
    if podman image exists "$image:previous"; then
        echo "Going back to the previous image." >&2
        podman tag "$image:previous" "$image:latest"
        restart_all
    fi
    exit 1
fi

podman image prune -f >/dev/null
echo "Updated to $commit; restarted: ${units[*]:-(no athletes yet)}."
echo "If the update changed how activities are read, the nightly run reprocesses them."
