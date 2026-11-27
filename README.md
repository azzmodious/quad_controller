# quad_controller

Raspberry Pi 5 companion-computer software for an ArduPilot Pixhawk 2.4.8 quadcopter. The Pixhawk flies the aircraft; the Pi handles telemetry, the dashboard, video, calibration, peripherals and missions. See [docs/PLAN.md](docs/PLAN.md) for the phased plan.

**Status: Phase 0 (foundation).** `quadctl` connects to the Pixhawk or to SITL over MAVLink and logs heartbeat, mode and battery. It never arms or changes modes.

## Quick start (SITL, Linux or WSL2)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,sitl]"
./scripts/sitl.sh --background
quadctl run --target sitl
```

## Commands

| Command | What it does |
| --- | --- |
| `quadctl run` | Connect and log status (the systemd service runs this) |
| `quadctl check` | Exit 0 if an autopilot heartbeat arrives within 5 s |
| `quadctl firmware-info` | ArduCopter version, board and boot banner |
| `quadctl logs [-f] [--level WARNING] [--grep text]` | Read the JSON-lines log |
| `quadctl config` | Print the resolved config |

Every command takes `--target sitl|hardware`, `--connection <mavlink url>` and `--config <file>`. Defaults are in [config/default.yaml](config/default.yaml); put your changes in `config/local.yaml` (git-ignored).

## Layout

```
src/quad_controller/   Python package (config, logging, MAVLink helpers, CLI)
config/                default.yaml
scripts/               sitl.sh, install_service.sh, deploy.sh, cloudflare_tunnel.sh
deploy/                systemd unit, cloudflared config template, Pixhawk param file
docs/hardware/         wiring, Pixhawk params, firmware record
docs/setup/            Pi setup checklist, SITL, remote access
tests/                 unit tests (fake autopilot) and SITL tests
```

## Setting up the real quad

1. [Wire the Pi to TELEM2](docs/hardware/wiring.md)
2. [Set the Pixhawk params](docs/hardware/pixhawk-params.md)
3. [Set up the Pi](docs/setup/pi-setup.md) and [record the firmware](docs/hardware/firmware.md)
4. [Cloudflare Tunnel and Access](docs/setup/remote-access.md)

Props stay off for all of Phase 0.

## Development

```bash
pip install -e ".[dev]"
pre-commit install
ruff check . && pytest
```
