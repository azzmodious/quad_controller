# Testing against ArduCopter SITL

SITL runs the real ArduCopter code on your computer with a simulated quad, so everything can be tried without the aircraft. The same `quadctl` commands work against SITL and the Pixhawk; only `--target` changes.

## Linux or WSL2

On Windows, use WSL2 (`wsl --install -d Ubuntu` in an admin PowerShell, then reboot). Everything below runs inside the Ubuntu shell. Clone the repo inside WSL (for example `~/quad_controller`) rather than working in `/mnt/d/...`, which is much slower.

```bash
sudo apt install -y python3-venv curl
cd ~/quad_controller
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,sitl]"

./scripts/sitl.sh --background     # downloads SITL the first time, then starts SITL + MAVProxy
quadctl check --target sitl         # "check_ok ... mode=STABILIZE"
quadctl telemetry --target sitl     # live terminal view; Ctrl+C to quit
quadctl run --target sitl           # status lines in the log, like the Pi service
./scripts/sitl.sh --stop
```

Run the SITL tests:

```bash
./scripts/sitl.sh --background
QC_SITL=1 pytest -m sitl
./scripts/sitl.sh --stop
```

### Watching along in Mission Planner or QGroundControl

MAVProxy also forwards to UDP 14551. In QGroundControl add a UDP link on port 14551. In Mission Planner pick **UDP**, port **14551**. From WSL2, Windows apps reach these ports through `localhost` on recent Windows 11 builds; if not, add `--out udp:<windows-ip>:14551` in `scripts/sitl.sh`.

Fly it from MAVProxy's console if you start it in the foreground (`./scripts/sitl.sh`), or from the GCS: `mode guided`, `arm throttle`, `takeoff 10`.

## Without WSL: Mission Planner's simulator

Mission Planner's **Simulation** tab can run ArduCopter SITL on Windows directly. Start a multirotor there, then point quadctl at it (from Windows Python):

```powershell
quadctl check --connection tcp:127.0.0.1:5760
```

## Options

| Env var | Default | Meaning |
| --- | --- | --- |
| `ARDUCOPTER_BIN` | download | Use your own SITL build |
| `SITL_VERSION` | `stable` | `stable`, `beta` or `latest` from firmware.ardupilot.org |
| `SITL_HOME` | ArduPilot test field | `lat,lon,alt,heading`. Set your own field to see it on the map later |
| `SITL_SPEEDUP` | `1` | Run the sim faster than real time |

`--no-mavproxy` skips MAVProxy; then connect straight to SITL with `--connection tcp:127.0.0.1:5760`. Files SITL writes (eeprom, logs) go to `.sitl/`, which git ignores. Delete `.sitl/eeprom.bin` to reset SITL's params.

## In CI

`.github/workflows/ci.yml` runs lint and unit tests, then starts SITL with `scripts/sitl.sh --background` and runs `pytest -m sitl`:

* Phase 0: a heartbeat within 5 seconds; mode and battery logged.
* Phase 1 (`tests/sitl/test_sitl_phase1.py`): our state matches an independent pymavlink reader on MAVProxy's second port; the test takes off to 10 m, flies a 25 s circle in GUIDED with velocity setpoints and checks heading, bank and position follow; it records the flight and replays it; then it freezes SITL with `SIGSTOP` and checks `link_lost` within 3 s and `link_restored` after `SIGCONT`.

The Phase 1 test flies, so run it on a fresh SITL (`./scripts/sitl.sh --stop`, delete `.sitl/eeprom.bin`, start again) if you run the SITL tests twice in a row.
