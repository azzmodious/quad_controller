#!/usr/bin/env bash
# Cloudflare Tunnel for the Pi: no open ports on your home router, nothing
# public until Cloudflare Access lets you in.
#
#   ./scripts/cloudflare_tunnel.sh install            install cloudflared from Cloudflare's apt repo
#   ./scripts/cloudflare_tunnel.sh create <domain>    log in, create tunnel "quad", DNS for
#                                                     ssh.<domain> and quad.<domain>, run as a service
#   ./scripts/cloudflare_tunnel.sh verify <domain>    check both hostnames demand an Access login
#   ./scripts/cloudflare_tunnel.sh ssh-ca <principal> "<ca public key>"
#                                                     trust Cloudflare's short-lived SSH certs for
#                                                     browser SSH; <principal> is the part of your
#                                                     Access email before the @
#   ./scripts/cloudflare_tunnel.sh status             service and tunnel status
#
# Run on the Pi as your normal user; it uses sudo where needed.
# Full walk-through, including the Access setup in the dashboard: docs/setup/remote-access.md
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TUNNEL_NAME="${TUNNEL_NAME:-quad}"

need_domain() {
  if [[ -z "${1:-}" ]]; then
    echo "usage: $0 $cmd <domain>   e.g. $0 $cmd example.com" >&2
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
  command -v cloudflared >/dev/null || do_install

  if [[ ! -f "$HOME/.cloudflared/cert.pem" ]]; then
    echo "A browser link will appear. Open it on any device, log in to Cloudflare,"
    echo "and pick the zone $domain."
    cloudflared tunnel login
  fi

  if ! cloudflared tunnel info "$TUNNEL_NAME" >/dev/null 2>&1; then
    cloudflared tunnel create "$TUNNEL_NAME"
  fi
  local id
  id="$(cloudflared tunnel list --name "$TUNNEL_NAME" --output json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"
  echo "Tunnel $TUNNEL_NAME has id $id"

  cloudflared tunnel route dns --overwrite-dns "$TUNNEL_NAME" "ssh.$domain"
  cloudflared tunnel route dns --overwrite-dns "$TUNNEL_NAME" "quad.$domain"

  sudo mkdir -p /etc/cloudflared
  sudo install -m 0600 "$HOME/.cloudflared/$id.json" "/etc/cloudflared/$id.json"
  sed -e "s|@TUNNEL_ID@|$id|g" -e "s|@DOMAIN@|$domain|g" \
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

  $0 verify $domain
MSG
}

do_verify() {
  local domain="$1" ok=1
  for host in "ssh.$domain" "quad.$domain"; do
    # Behind Access, an anonymous request is redirected to the Access login page.
    local code location
    code="$(curl -s -o /dev/null -w '%{http_code}' "https://$host/")"
    location="$(curl -s -o /dev/null -w '%{redirect_url}' "https://$host/")"
    if [[ "$code" =~ ^30[0-9]$ && "$location" == *cloudflareaccess.com* ]]; then
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
  systemctl --no-pager --lines=10 status cloudflared || true
  cloudflared tunnel info "$TUNNEL_NAME" || true
}

cmd="${1:-}"
case "$cmd" in
  install) do_install ;;
  create) need_domain "${2:-}"; do_create "$2" ;;
  verify) need_domain "${2:-}"; do_verify "$2" ;;
  ssh-ca)
    if [[ $# -lt 3 ]]; then
      echo "usage: $0 ssh-ca <principal> \"<ca public key>\"" >&2
      exit 2
    fi
    do_ssh_ca "$2" "$3" ;;
  status) do_status ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
