# Pi 5 setup checklist

Do these in order on the bench, **props off**. Each section ends with a check so you know it worked before moving on.

Before you start: wiring done per [wiring.md](../hardware/wiring.md) and Pixhawk params set per [pixhawk-params.md](../hardware/pixhawk-params.md).

## 1. Flash Raspberry Pi OS

- [ ] Use Raspberry Pi Imager on your PC. Choose **Raspberry Pi 5** and **Raspberry Pi OS Lite (64-bit)**, Bookworm or newer.
- [ ] In the Imager's settings (gear / "Edit settings"): set the hostname to `quad`, create your user, add your Wi-Fi, and **enable SSH with public-key auth** (paste your laptop's public key).
- [ ] Boot the Pi and SSH in from home: `ssh <user>@quad.local`.
- [ ] Update: `sudo apt update && sudo apt full-upgrade -y && sudo reboot`

**Check:** `ssh <user>@quad.local` works without a password.

## 2. Turn on the GPIO UART

On the Pi 5, GPIO 14/15 is UART0 and shows up as `/dev/ttyAMA0` once enabled. (The small 3-pin debug connector is a different UART, `/dev/ttyAMA10`.)

- [ ] Turn off the serial login console and turn the serial hardware on:
  ```bash
  sudo raspi-config nonint do_serial_cons 1   # no login shell on serial
  sudo raspi-config nonint do_serial_hw 0     # enable the UART
  ```
- [ ] Make sure `/boot/firmware/config.txt` has this line (add it at the end if not):
  ```
  dtparam=uart0=on
  ```
- [ ] `sudo reboot`

**Check:** `ls -l /dev/ttyAMA0` shows the device. `groups` lists `dialout` (the install script below adds it if not).

## 3. Get the code and install the service

```bash
sudo apt install -y git
git clone https://github.com/azzmodious/quad_controller.git ~/quad_controller
cd ~/quad_controller
sudo ./scripts/install_service.sh
```

This creates `.venv`, installs the package, installs and starts the `quad-controller` systemd service (`quadctl run --target hardware`), and lets your user restart it without a password.

**Check:** with the Pixhawk powered (USB or battery, props off):

```bash
~/quad_controller/.venv/bin/quadctl logs -f
```

You should see `link_up` and then a `status` line every 2 seconds with the mode, armed state and battery voltage. If you see only `waiting_for_heartbeat`, swap the TX and RX wires (Pi pins 8 and 10) and check `SERIAL2_BAUD=921`.

## 4. Record the firmware

```bash
sudo systemctl stop quad-controller   # free the serial port for a moment
~/quad_controller/.venv/bin/quadctl firmware-info --target hardware
sudo systemctl start quad-controller
```

Copy the output into [firmware.md](../hardware/firmware.md) and commit it.

## 5. Remote access

Follow [remote-access.md](remote-access.md) to put the Pi behind Cloudflare Tunnel and Access.

## 6. Bench test (end of Phase 0)

- [ ] Props off.
- [ ] Power the Pi from its BEC and the Pixhawk from its battery, as they will be in flight.
- [ ] Reboot the Pi. The service starts by itself: `systemctl status quad-controller` is `active (running)`.
- [ ] Heartbeat, mode and battery voltage appear in `quadctl logs -f`.
- [ ] Unplug the TELEM2 cable: a `link_lost` warning appears within 5 seconds. Plug it back: `link_up` again.
- [ ] Kill the process (`sudo pkill -f "quadctl run"`): systemd restarts it within a few seconds.
- [ ] From your phone on **cellular** (Wi-Fi off), open `https://ssh.<your domain>`, log in through Access, and run `quadctl logs -f` in the browser terminal.
- [ ] Arm and disarm once **from the RC transmitter** to confirm nothing changed there.

## Updating later

From the Pi, or from your laptop over the tunnel:

```bash
ssh drone01 '~/quad_controller/scripts/deploy.sh'          # latest main (Host name from your ssh config)
ssh drone01 '~/quad_controller/scripts/deploy.sh my-branch' # try a branch
```

## Handy commands

| What | Command |
| --- | --- |
| Live log, readable | `quadctl logs -f` |
| Only warnings and errors | `quadctl logs --level WARNING` |
| Last 50 lines mentioning the battery | `quadctl logs -n 50 --grep voltage` |
| systemd's view | `journalctl -u quad-controller -f` |
| Restart | `sudo systemctl restart quad-controller` |
| Resolved config | `quadctl config --target hardware` |

The JSON log lives in `/var/log/quad_controller/quad_controller.jsonl`, rotated at 5 MB with 10 old files kept. Pixhawk DataFlash logs stay on the Pixhawk's SD card; we pull those in a later phase.
