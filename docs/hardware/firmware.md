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

Fill this in after running the check. Paste the raw output at the bottom.

| Field | Value |
| --- | --- |
| Date checked | |
| Board name in banner | |
| Firmware build | |
| ArduCopter version | |
| Git hash | |
| Flash | 1 MB / 2 MB |
| Vendor / product id | |
| Features missing (1 MB only) | |

```
(paste `quadctl firmware-info --target hardware` output here)
```
