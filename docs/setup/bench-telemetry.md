# Bench telemetry check (Phase 1)

**Props off.** Nothing here arms the aircraft: against the hardware target, `quadctl` refuses arm, mode, takeoff and motor commands.

You need: the Pixhawk on the Pi (USB during development, TELEM2 before flight), SSH to the Pi (`drone01-ssh.casa-bakewell.com` works from the phone too), and optionally Mission Planner or QGroundControl on the laptop.

## 1. Update the Pi

```bash
cd ~/quad_controller && scripts/deploy.sh
```

## 2. Free the port

The `quad-controller` service holds the Pixhawk's port. Stop it while you use the live view, and start it again afterwards.

```bash
sudo systemctl stop quad-controller
```

## 3. Live view

```bash
.venv/bin/quadctl telemetry --target hardware
```

The top line shows the link (green UP, yellow DEGRADED, red LOST), the mode and DISARMED. Tilt the quad by hand:

* right side down: roll goes positive and the horizon rises on the right
* nose up: pitch goes positive and the horizon drops
* turn it on the bench: the compass tape and heading follow

Indoors expect no GPS fix and EKF `pos ✗`; that is normal. Ctrl+C quits.

On a narrow screen (the phone) the panels stack; scroll if needed, or use `--no-screen`.

## 4. Link-lost test

With the live view open, unplug the USB cable (or the TELEM2 lead). Within 3 s the top line turns red **LOST**. Plug it back in: it reconnects by itself and goes green again. On USB the Pixhawk reboots when it loses power, so the first seconds after replugging can show DEGRADED while it starts.

## 5. Record and replay

```bash
.venv/bin/quadctl record --target hardware --duration 30 --out ~/bench.jsonl.gz
.venv/bin/quadctl replay ~/bench.jsonl.gz
```

Tilt the quad while it records; the replay shows the same movement. Recordings without `--out` go to the `recordings` folder next to the logs.

## 6. Start the service again

```bash
sudo systemctl start quad-controller
.venv/bin/quadctl logs -n 5
```

The status lines now include roll, pitch, heading, GPS fix and satellites.

## Side by side with Mission Planner (optional)

Connect Mission Planner to the Pixhawk's other port (USB to the laptop, or a telemetry radio) and compare roll, pitch, heading and battery voltage with the live view. They should agree within a degree or two.

## If something looks wrong

| Symptom | Likely cause |
| --- | --- |
| "No heartbeat yet" hint | The service is still running (step 2), or the wrong port in `config/local.yaml` |
| Roll and pitch never move, link UP | Streams not arriving: `quadctl logs --grep stream` |
| Values freeze, link goes yellow then red | Cable or power; check `vcgencmd get_throttled` on the Pi |
