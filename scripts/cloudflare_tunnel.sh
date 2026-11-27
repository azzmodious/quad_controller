#!/usr/bin/env bash
# Cloudflare Tunnel for the Pi: no open ports on your home router, nothing
# public until Cloudflare Access lets you in.
#
#   ./scripts/cloudflare_tunnel.sh install                   install cloudflared from Cloudflare's apt repo
#   ./scripts/cloudflare_tunnel.sh create <domain> [name]    log in, create a tunnel called <name>, DNS for
#                                                            <name>.<domain> (dashboard) and
#                                                            <name>-ssh.<domain> (SSH), run as a service
#   ./scripts/cloudflare_tunnel.sh verify <domain> [name]    check both hostnames demand an Access login
#   ./scripts/cloudflare_tunnel.sh ssh-ca <principal> "<ca public key>"
#                                                            trust Cloudflare's short-lived SSH certs for
#                                                            browser SSH; <principal> is the part of
#                                                            your Access email before the @
#   ./scripts/cloudflare_tunnel.sh status [name]             service and tunnel status
#
# <name> is this drone's name and defaults to "quad". Example:
#   ./scripts/cloudflare_tunnel.sh create example.com drone01
#   -> drone01.example.com and drone01-ssh.example.com
# One level of subdomain keeps both names covered by Cloudflare's free certificate.
#
# Run on the Pi as your normal user; it uses sudo where needed.
# Full walk-through, including the Access setup in the dashboard: docs/setup/remote-access.md
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_NAME="quad"

# Sets NAME, DASH_HOST and SSH_HOST from <domain> [name].
set_names() {
  local domain="$1"
  NAME="${2:-$DEFAULT_NAME}"
  if [[ ! "$NAME" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
    echo "name must be lowercase letters, digits and dashes, e.g. drone01" >&2
    exit 2
  fi
  DASH_HOST="$NAME.$domain"
  SSH_HOST="$NAME-ssh.$domain"
}

need_domain() {
  if [[ -z "${1:-}" ]]; then
    echo "usage: $0 $cmd <domain> [name]   e.g. $0 $cmd example.com drone01" >&2
    exit 2
  fi
}

do_install() {
  if command -v cloudflared >/dev/null; then
    echo "cloudflared already installed: $(cloudflared --version)"
    return
  fi
  sudo mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
    | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y cloudflared
  cloudflared --version
}

do_create() {
  local domain="$1"
  set_names "$1" "${2:-}"
  command -v cloudflared >/dev/null || do_install

  if [[ ! -f "$HOME/.cloudflared/cert.pem" ]]; then
    echo "A browser link will appear. Open it on any device, log in to Cloudflare,"
    echo "and pick the zone $domain."
    cloudflared tunnel login
  fi

  if ! cloudflared tunnel info "$NAME" >/dev/null 2>&1; then
    cloudflared tunnel create "$NAME"
  fi
  local id
  id="$(cloudflared tunnel list --name "$NAME" --output json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"
  echo "Tunnel $NAME has id $id"

  cloudflared tunnel route dns --overwrite-dns "$NAME" "$SSH_HOST"
  cloudflared tunnel route dns --overwrite-dns "$NAME" "$DASH_HOST"

  sudo mkdir -p /etc/cloudflared
  sudo install -m 0600 "$HOME/.cloudflared/$id.json" "/etc/cloudflared/$id.json"
  sed -e "s|@TUNNEL_ID@|$id|g" -e "s|@SSH_HOST@|$SSH_HOST|g" -e "s|@DASH_HOST@|$DASH_HOST|g" \
    "$ROOT/deploy/cloudflared/config.yml.template" | sudo tee /etc/cloudflared/config.yml >/dev/null
  sudo cloudflared --config /etc/cloudflared/config.yml ingress validate

  if systemctl cat cloudflared.service >/dev/null 2>&1; then
    sudo systemctl restart cloudflared
  else
    sudo cloudflared --config /etc/cloudflared/config.yml service install
  fi
  sudo systemctl enable --now cloudflared
  sleep 3
  systemctl --no-pager --lines=5 status cloudflared || true

  cat <<MSG

Tunnel is up. Before you use it, protect both hostnames with Cloudflare Access
(docs/setup/remote-access.md, "Cloudflare Access"). Then run:

  $0 verify $domain $NAME

Hostnames:  dashboard  https://$DASH_HOST
            SSH        https://$SSH_HOST
MSG
}

do_verify() {
  local ok=1
  set_names "$1" "${2:-}"
  for host in "$SSH_HOST" "$DASH_HOST"; do
    # Behind Access, an anonymous request is redirected to the Access login page.
    local out rc=0 code location
    out="$(curl -s -o /dev/null --max-time 15 -w '%{http_code} %{redirect_url}' "https://$host/")" || rc=$?
    code="${out%% *}"
    location="${out#* }"
    if [[ $rc -ne 0 ]]; then
      echo "FAIL  $host could not be reached (curl exit $rc). Check DNS and that cloudflared is running."
      ok=0
    elif [[ "$code" =~ ^30[0-9]$ && "$location" == *cloudflareaccess.com* ]]; then
      echo "OK    $host requires an Access login"
    else
      echo "FAIL  $host answered HTTP $code without an Access login. Set up Access before going further."
      ok=0
    fi
  done
  [[ $ok -eq 1 ]]
}

do_ssh_ca() {
  local principal="$1" ca_key="$2" user
  user="$(id -un)"
  if [[ "$ca_key" != ssh-* && "$ca_key" != ecdsa-* ]]; then
    echo "the CA key should start with ecdsa-sha2-nistp256 or ssh-..." >&2
    exit 2
  fi
  echo "$ca_key" | sudo tee /etc/ssh/cloudflare_access_ca.pub >/dev/null
  sudo mkdir -p /etc/ssh/auth_principals
  echo "$principal" | sudo tee "/etc/ssh/auth_principals/$user" >/dev/null
  sudo tee /etc/ssh/sshd_config.d/50-cloudflare-access.conf >/dev/null <<CONF
# Accept Cloudflare Access short-lived certificates (browser SSH).
# Normal authorized_keys logins keep working.
TrustedUserCAKeys /etc/ssh/cloudflare_access_ca.pub
AuthorizedPrincipalsFile /etc/ssh/auth_principals/%u
CONF
  sudo sshd -t
  sudo systemctl reload ssh
  echo "sshd now accepts Cloudflare certs for principal '$principal' as user '$user'."
}

do_status() {
  NAME="${1:-$DEFAULT_NAME}"
  systemctl --no-pager --lines=10 status cloudflared || true
  cloudflared tunnel info "$NAME" || true
}

cmd="${1:-}"
case "$cmd" in
  install) do_install ;;
  create) need_domain "${2:-}"; do_create "$2" "${3:-}" ;;
  verify) need_domain "${2:-}"; do_verify "$2" "${3:-}" ;;
  ssh-ca)
    if [[ $# -lt 3 ]]; then
      echo "usage: $0 ssh-ca <principal> \"<ca public key>\"" >&2
      exit 2
    fi
    do_ssh_ca "$2" "$3" ;;
  status) do_status "${2:-}" ;;
  *) sed -n '2,21p' "$0"; exit 2 ;;
esac
