# Remote access: Cloudflare Tunnel + Access

The Pi opens an outbound tunnel to Cloudflare, so nothing on your home router is opened. Cloudflare Access sits in front of both hostnames and only lets your email through. Anything that can arm the quad in later phases sits behind this gate.

`<name>` is the drone's name, such as `drone01`; it defaults to `quad`. Both hostnames stay one level below your domain so Cloudflare's free certificate covers them.

| Hostname | Goes to | Used for |
| --- | --- | --- |
| `<name>-ssh.<domain>` | Pi port 22 | SSH from the laptop, or a terminal in any browser |
| `<name>.<domain>` | Pi port 8000 | The dashboard (Phase 2). Shows a 502 behind the login until then |

## What you need

- [ ] A domain whose DNS is on Cloudflare (the free plan is fine). A cheap domain works; it only hosts these two names.
- [ ] A Cloudflare Zero Trust organization (free for up to 50 users). The first visit to **one.dash.cloudflare.com** asks you to pick a team name and the Free plan.
- [ ] Phase 0 Pi setup through step 3 of [pi-setup.md](pi-setup.md).

## 1. Create the tunnel (on the Pi, at home)

```bash
cd ~/quad_controller
./scripts/cloudflare_tunnel.sh create <domain> <name>   # e.g. example.com drone01
```

It installs `cloudflared`, prints a login link (open it on any device and pick your domain), creates a tunnel named after the drone, adds the two DNS records, writes `/etc/cloudflared/config.yml` from [`deploy/cloudflared/config.yml.template`](../../deploy/cloudflared/config.yml.template), and runs `cloudflared` as a systemd service.

**Check:** `./scripts/cloudflare_tunnel.sh status <name>` shows the service `active (running)` and the tunnel with connections.

> The hostnames are reachable as soon as this finishes. Do step 2 right away, before you leave home.

## 2. Put both hostnames behind Cloudflare Access (in the dashboard)

Cloudflare moves these menus around now and then; if a name differs, search the Zero Trust dashboard for "Applications".

- [ ] Zero Trust dashboard > **Access > Applications > Add an application > Self-hosted**.
- [ ] Name it `<name> SSH`. Add the public hostname `<name>-ssh.<domain>`.
- [ ] Add a policy: name `Me`, action **Allow**, include **Emails** = your email address.
- [ ] Login methods: leave **One-time PIN** on (Cloudflare emails you a code). Add Google or GitHub later if you prefer.
- [ ] Session duration: 24 hours is a good start.
- [ ] Under the application's advanced or browser settings, set **Browser rendering** to **SSH**. Save.
- [ ] Add a second self-hosted application, `<name> dashboard`, for `<name>.<domain>` with the same `Me` policy. Leave browser rendering off.

**Check:** on the Pi, run

```bash
./scripts/cloudflare_tunnel.sh verify <domain> <name>
```

Both lines must say `OK ... requires an Access login`. Also open `https://<name>.<domain>` in a private browser window: you should see the Cloudflare Access login page, not anything from the Pi.

## 3. Browser SSH (works from any computer or phone)

Browser SSH logs in with a short-lived certificate that Cloudflare issues after the Access login, so no password or key is needed on the device you are using.

- [ ] Zero Trust dashboard: find **SSH short-lived certificates** (under Access service credentials or the `<name> SSH` app's settings), pick the `<name> SSH` app and **Generate certificate**. Copy the public key it shows (starts with `ecdsa-sha2-nistp256`).
- [ ] On the Pi:
  ```bash
  ./scripts/cloudflare_tunnel.sh ssh-ca <email-name> "<paste the public key>"
  ```
  `<email-name>` is your Access email before the `@` (for `sam@example.com` it is `sam`). The script maps it to your Pi user, so they do not need to match.
- [ ] Open `https://<name>-ssh.<domain>` in a browser, log in through Access, and you land in a terminal on the Pi.

Cloudflare's reference: https://developers.cloudflare.com/cloudflare-one/identity/users/short-lived-certificates/

## 4. SSH from your Windows laptop

- [ ] Install cloudflared: `winget install --id Cloudflare.cloudflared` in PowerShell. Run `where.exe cloudflared` to get its path.
- [ ] Add this to `C:\Users\<you>\.ssh\config`, using the path from the previous step:
  ```
  Host <name>
    HostName <name>-ssh.<domain>
    User <your Pi user>
    ProxyCommand "C:\Program Files (x86)\cloudflared\cloudflared.exe" access ssh --hostname %h
  ```
- [ ] `ssh <name>`. The first time, a browser window opens for the Access login; after that the session lasts until it expires.

This uses your normal SSH key, so it works with or without step 3.

**Check (the Phase 0 demo):** away from home, or on your phone's cellular connection, open `https://<name>-ssh.<domain>`, log in, and run `~/quad_controller/.venv/bin/quadctl logs -f` to watch the Pixhawk's heartbeat, mode and battery voltage.

## If something goes wrong

| Symptom | Likely cause |
| --- | --- |
| `verify` says FAIL | The Access application is missing, or its hostname does not match exactly |
| Browser shows Cloudflare error 1033 | `cloudflared` is not running on the Pi: `sudo systemctl status cloudflared` |
| Browser SSH asks for a password | Step 3 is not done, or the principal does not match your email name |
| `<name>.<domain>` shows 502 after login | Expected until the Phase 2 dashboard listens on port 8000 |

## Safety

- Never remove the Access applications or set a policy to "Everyone" or "Bypass". Later phases rely on Access as the outer gate before any arm or motor control.
- Keep normal SSH key auth on the Pi. Do not turn on password login for SSH.
- If a laptop or phone is lost, revoke its sessions in Zero Trust under **My Team > Users**.
