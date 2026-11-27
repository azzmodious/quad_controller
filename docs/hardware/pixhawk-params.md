# Pixhawk params for the companion link

These three params turn TELEM2 into a fast MAVLink 2 port for the Pi. They do not touch arming, failsafes or flight tuning.

| Param | Value | Why |
| --- | --- | --- |
| `SERIAL2_PROTOCOL` | `2` | MAVLink 2 on TELEM2 |
| `SERIAL2_BAUD` | `921` | 921600 baud. Drop to `115` (115200) if you see corrupt packets on long wires |
| `BRD_SER2_RTSCTS` | `0` | No flow control, because we only wire TX, RX and GND |

The same values are in [`deploy/ardupilot/companion-telem2.param`](../../deploy/ardupilot/companion-telem2.param).

We do not set the `SR2_*` stream-rate params. The Pi asks for the messages it needs with `MAV_CMD_SET_MESSAGE_INTERVAL`, so the rates live in our code and survive a param reset.

## Checklist (Mission Planner on your laptop, Pixhawk on USB)

- [ ] Props are off.
- [ ] Connect the Pixhawk to the laptop by USB and connect in Mission Planner.
- [ ] Save a backup first: **Config > Full Parameter List > Save**. Name it with today's date and keep it in a safe place.
- [ ] **Load from file** and pick `deploy/ardupilot/companion-telem2.param`, then **Write Params**. Or set the three values by hand in the list.
- [ ] Reboot the Pixhawk (**Ctrl+F > Reboot Pixhawk**, or unplug and replug USB).
- [ ] After the reboot, check the three values stuck.
- [ ] Confirm arming still only works from the RC transmitter: arm and disarm once with the sticks, props off.

QGroundControl works too: **Vehicle Setup > Parameters**, search each name, set, then **Tools > Reboot Vehicle**.

## If the 2.4.8 is a 1 MB board

Some param names move between ArduPilot versions. If `BRD_SER2_RTSCTS` is missing, search for `SERIAL2_OPTIONS` or `BRD_SER` and leave flow control off. Record what you found in [firmware.md](firmware.md).
