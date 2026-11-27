# quad_controller

Raspberry Pi 5 companion-computer software for an ArduPilot Pixhawk 2.4.8 quadcopter. The Pixhawk flies the aircraft; the Pi handles telemetry, the dashboard, video, calibration, peripherals and missions. See [docs/PLAN.md](docs/PLAN.md) for the phased plan.

**Status: Phase 1 (link and telemetry).** `quadctl` keeps a MAVLink link to the Pixhawk or SITL alive (our heartbeat, stream requests, reconnects, a watchdog), keeps a typed vehicle state, and shows it live in the terminal. Against real hardware it never arms or changes modes; flight commands are only allowed against SITL.

## Quick start (SITL, Linux or WSL2)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,sitl]"
./scripts/sitl.sh --background
quadctl telemetry --target sitl     # live view; Ctrl+C to quit
```

## Commands

| Command | What it does |
| --- | --- |
| `quadctl run [--record]` | Keep the link up and log status (the systemd service runs this) |
| `quadctl telemetry [--record]` | Live terminal view: horizon, heading, GPS, battery, EKF, RC, messages |
| `quadctl record [--out f] [--duration s]` | Save telemetry at 10 Hz (`.jsonl` or `.jsonl.gz`) |
| `quadctl replay <file> [--speed 2]` | Play a recording back in the terminal view |
| `quadctl check` | Exit 0 if an autopilot heartbeat arrives within 5 s |
| `quadctl firmware-info` | ArduCopter version, board and boot banner |
| `quadctl logs [-f] [--level WARNING] [--grep text]` | Read the JSON-lines log |
| `quadctl config` | Print the resolved config |

Every command takes `--target sitl|hardware`, `--connection <mavlink url>` and `--config <file>`. Defaults are in [config/default.yaml](config/default.yaml); put your changes in `config/local.yaml` (git-ignored).

## Layout

```
src/quad_controller/   Python package
  link.py              async MAVLink link layer (heartbeat, streams, reconnect)
  state.py             typed vehicle state with timestamps
  watchdog.py          link up / degraded / lost
  commands.py          COMMAND_LONG with retries and ACKs, flight-command guard
  recorder.py          10 Hz telemetry recorder and replay
  tui.py               terminal telemetry view
  cli.py, config.py, logging_setup.py, mavlink.py
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
5. [Bench telemetry check](docs/setup/bench-telemetry.md) (Phase 1)

Props stay off. Nothing in Phase 0 or Phase 1 arms the real aircraft.

## Development

```bash
pip install -e ".[dev]"
pre-commit install
ruff check . && pytest
```
