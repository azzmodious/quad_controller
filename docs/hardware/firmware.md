# Pixhawk board and firmware record

Many Pixhawk 2.4.8 boards are clones. Some have a chip that only exposes **1 MB** of flash, and those need the `Pixhawk1-1M` ArduCopter build, which leaves out some features. We check once and write the answer here so later phases know what they can rely on.

## How to check

From the Pi once the wiring and params are done (or from any computer with the Pixhawk on USB, using `--connection COM5` on Windows or `--connection /dev/ttyACM0` on Linux):

```bash
quadctl firmware-info --target hardware
```

It prints the ArduCopter version, git hash, board and vendor ids, and the boot banner. The banner names the firmware build (`Pixhawk1`, `Pixhawk1-1M`, `fmuv3` and so on), and `flash_hint` says what that means.

In Mission Planner you can also read the banner in the **Messages** tab right after connecting.

## If it is a 1 MB board

- [ ] Flash the `Pixhawk1-1M` ArduCopter build: Mission Planner **Setup > Install Firmware**, pick the board manually if it is not detected.
- [ ] Note below which features the 1M build lacks that later phases want (precision landing, scripting, some rangefinder drivers). The ArduPilot firmware page lists what each build includes.

## Record

Checked on the bench over USB on 2026-11-27.

| Field | Value |
| --- | --- |
| Date checked | 2026-11-27 |
| Board name in banner | `PX4v3` (fmuv3) |
| Firmware build | ArduCopter for PX4v3 (NuttX) |
| ArduCopter version | 3.6.12 (official, 2019) |
| Git hash | cb570c06 |
| Flash | 2 MB: the fmuv3 build only runs on boards with the full 2 MB |
| Vendor / product id | not reported by 3.6 |
| Features missing (1 MB only) | none, this is a 2 MB board |

Notes:

- 3.6 is old. A current release would be the `Pixhawk1` ArduCopter 4.x build, not `Pixhawk1-1M`. Upgrading means redoing calibration, which Phase 4 covers anyway.
- 3.6 ignores `MAV_CMD_REQUEST_MESSAGE`; `quadctl` also sends `MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES` for it.
- Connected for development over USB as `/dev/serial/by-id/usb-3D_Robotics_PX4_FMU_v2.x_0-if00`. Fly on TELEM2.

```
banner:
  ArduCopter V3.6.12 (cb570c06)
  PX4: 1e7ed30b NuttX: 1472b16c
  PX4v3 00280025 35335105 33353832
  Frame: QUAD
firmware_version: 3.6.12-official
board_version: 0
vendor_id: 0
product_id: 0
uid: 0000000000000000
git_hash: cb570c0
capabilities: 15311
flash_hint: 2 MB flash build: full feature set
```
