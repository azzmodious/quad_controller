#!/usr/bin/env bash
# Update the Pi to the latest code and restart the service.
#
# On the Pi:          ~/quad_controller/scripts/deploy.sh
# From your laptop:   ssh drone01 '~/quad_controller/scripts/deploy.sh'
#
# Optional: deploy.sh <branch> checks out that branch first.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

branch="${1:-}"
before="$(git rev-parse --short HEAD)"
git fetch --quiet origin
if [[ -n "$branch" ]]; then
  git checkout --quiet "$branch"
fi
git pull --ff-only --quiet
after="$(git rev-parse --short HEAD)"

.venv/bin/pip install --quiet -e .
sudo systemctl restart quad-controller
sleep 2
sudo systemctl status quad-controller --no-pager --lines=5 || true
echo "Deployed $before -> $after on branch $(git rev-parse --abbrev-ref HEAD)"
