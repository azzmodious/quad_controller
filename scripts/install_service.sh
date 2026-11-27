#!/usr/bin/env bash
# One-time setup on the Pi: create the venv, install the package and the systemd service.
#
#   cd ~/quad_controller && sudo ./scripts/install_service.sh
#
# Safe to run again; it updates the venv and unit file in place.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run with sudo: sudo $0" >&2
  exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_AS="${SUDO_USER:-$(stat -c %U "$REPO")}"
UNIT=/etc/systemd/system/quad-controller.service

echo "Repo: $REPO   user: $RUN_AS"

apt-get install -y --no-install-recommends python3-venv python3-dev >/dev/null
usermod -aG dialout "$RUN_AS"

sudo -u "$RUN_AS" bash -c "
  cd '$REPO'
  [[ -d .venv ]] || python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e .
"

sed -e "s|@USER@|$RUN_AS|g" -e "s|@REPO@|$REPO|g" \
  "$REPO/deploy/systemd/quad-controller.service" > "$UNIT"

# Let the deploy script restart the service without a password prompt.
cat > /etc/sudoers.d/quad-controller <<SUDO
$RUN_AS ALL=(root) NOPASSWD: /usr/bin/systemctl restart quad-controller, /usr/bin/systemctl status quad-controller
SUDO
chmod 0440 /etc/sudoers.d/quad-controller

systemctl daemon-reload
systemctl enable --now quad-controller
sleep 2
systemctl --no-pager --lines=5 status quad-controller || true
echo
echo "Installed. Watch it with:  journalctl -u quad-controller -f"
echo "                     or:  $REPO/.venv/bin/quadctl logs -f"
