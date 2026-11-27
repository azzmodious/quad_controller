# Quadcopter Flight Controller: Phased Implementation Plan

Nov 27, 2026 · Maximus · Source: [Claude doc](https://claude.ai/artifact/KbejKnyWNzSnZVv7zhfJbh)

The Pixhawk keeps flying the aircraft; the Pi 5 becomes the brain around it. Ten phases take the project from an empty repo to autonomous missions, and each ends with a demo you can show someone. Items marked **(guess)** are my assumptions, not decisions you made.

## Overview

**What we are building.** A companion-computer stack on the Raspberry Pi 5 that talks MAVLink to an ArduPilot Pixhawk 2.4.8. The Pixhawk runs ArduCopter and owns stabilization, motor mixing and the low-level failsafes. The Pi owns everything a person sees or a mission decides: telemetry, the web dashboard, video, calibration, peripherals, missions and later autonomy.

**Hardware.**

| Part | Connects to | Role |
| --- | --- | --- |
| Raspberry Pi 5 | Pixhawk TELEM2 (UART) | Companion computer, web server, video, peripherals |
| Pixhawk 2.4.8 | ESCs, GPS, RC receiver | ArduCopter autopilot, stabilization, motor output |
| Pi Camera | Pi CSI port | Live video, photos, later precision landing |
| LiDAR | Pi or Pixhawk (open question) | Altitude and obstacle ranging |
| Standalone IMU | Pi I2C/SPI | Role open: logging, cross-check, or vision fusion |
| Servo egg dropper | Pi GPIO hardware PWM | First peripheral plugin |
| RC transmitter + receiver | Pixhawk RCIN | Manual control, always wins |

**Control priority.** The RC transmitter always overrides. The laptop dashboard and autonomous code only send commands through ArduPilot modes (GUIDED, AUTO, RTL, LAND), so a flip of the RC mode switch takes control back at any time. The Pi never writes raw motor outputs.

_System architecture diagram (Pi modules, Pixhawk, remote access) lives in the [source doc](https://claude.ai/artifact/KbejKnyWNzSnZVv7zhfJbh)._

You reach the Pi only through Cloudflare; the Pi commands the Pixhawk over MAVLink, and the RC transmitter talks to the Pixhawk directly.

**Stack (guess, adjustable).**

- Python 3.11+ on Raspberry Pi OS Bookworm 64-bit
- `pymavlink` for MAVLink, wrapped in our own async link layer
- FastAPI + WebSockets for the dashboard backend; a small Svelte or plain TypeScript frontend
- `picamera2` with WebRTC or MJPEG for video
- `gpiozero` / `rpi-hardware-pwm` for servos on the Pi 5
- ArduCopter SITL (with MAVProxy) on the laptop or in CI for testing
- `systemd` services, `cloudflared` for remote access, `pytest` for tests

**How to read each phase.** Every phase lists its goal, the fun demo at the end, tasks sized for one work thread each, how to test it in SITL and on hardware, and safety notes. A task is roughly one pull request.

**Pixhawk 2.4.8 firmware check.** Many 2.4.8 boards are clones with the 1 MB flash bug. Recent ArduCopter builds may not fit or may drop features. Phase 0 confirms the board, the firmware version, and which features are present before we depend on them.

## Phase 0: Foundation

**Goal.** A Pi you can reach from anywhere, wired to the Pixhawk, with a Python project that runs the same code against SITL and the real aircraft.

**Fun demo.** From a coffee shop, open a browser to `quad.yourdomain.com`, log in through Cloudflare Access, and watch a terminal on the Pi print the Pixhawk's heartbeat, mode and battery voltage.

**Tasks.**

1. **Wiring and Pixhawk params.** Pixhawk TELEM2 to Pi GPIO 14/15 UART (TX to RX, RX to TX, shared ground, no 5V between them). Set `SERIAL2_PROTOCOL=2` (MAVLink2) and `SERIAL2_BAUD=921`. Enable `dtparam=uart0=on` on the Pi 5 so the port appears as `/dev/ttyAMA0` **(guess on exact overlay; verify on the board)**. Power the Pi from its own 5V 5A BEC, not the Pixhawk.
2. **Firmware check.** Read the board ID, ArduCopter version and flash size; record them in the repo. If it is a 1 MB clone, pick a known-good ArduCopter build and note which features are missing.
3. **Python project skeleton.** `pyproject.toml`, `src/quad_controller/` package, config file (YAML), `ruff`, `pytest`, pre-commit, and a GitHub Actions workflow that runs tests.
4. **SITL setup.** Script that starts ArduCopter SITL plus MAVProxy and exposes a UDP port; a `--target sitl|hardware` flag picks the connection string. Document it so it runs on your laptop and in CI.
5. **Logging.** Structured logs (JSON lines) with rotation on the Pi; a log viewer command. Pixhawk DataFlash logs stay on the SD card and get pulled later.
6. **Service install.** `systemd` unit for the controller, auto-restart on crash, and a one-command deploy script (`git pull` + restart).
7. **Cloudflare Tunnel and Access.** Install `cloudflared` as a service, map `ssh.yourdomain.com` and `quad.yourdomain.com` to the Pi. Put both behind Cloudflare Access with your email or a one-time PIN, so nothing is exposed to the open internet. Add browser SSH so you can work from any machine.

**Test in SITL.** CI starts SITL, the skeleton connects, receives a heartbeat within 5 seconds, and exits cleanly.

**Test on hardware.** On the bench with props off: Pi boots, service starts, heartbeat arrives over TELEM2, tunnel is reachable from your phone's cellular connection.

**Safety notes.** Props stay off for all of Phase 0. Confirm the Pixhawk still arms only from RC. Cloudflare Access is mandatory before any later phase exposes arm or motor controls.

## Phase 1: Link and telemetry

**Goal.** A reliable MAVLink link layer that every later feature builds on, with live state you can trust.

**Fun demo.** Tilt the quad by hand on the bench and watch roll, pitch and heading change live in a terminal dashboard (Rich or Textual), with GPS fix, satellites and battery updating.

**Tasks.**

1. **Link layer.** Async wrapper over `pymavlink`: connect, send our own heartbeat as a companion computer, request message streams at set rates, reconnect on drop.
2. **Vehicle state model.** One typed object (attitude, position, GPS, battery, mode, armed, EKF status, RC channels) updated from incoming messages, with timestamps so stale data is visible.
3. **Link watchdog.** Track heartbeat age both ways; mark the link degraded at 1 s and lost at 3 s **(guess on thresholds)**; emit events other modules can subscribe to.
4. **Command helper.** Send `COMMAND_LONG` with retries and wait for `COMMAND_ACK`, so later phases get a clear success or failure.
5. **Terminal telemetry view.** A small TUI for bench work and debugging over SSH.
6. **Telemetry recorder.** Save state to a local file at 10 Hz for replay in tests.

**Test in SITL.** Fly SITL in a circle from MAVProxy and assert our state matches; kill the SITL process and assert the watchdog fires; replay a recording in a unit test.

**Test on hardware.** Bench, props off, battery connected: values match Mission Planner or QGroundControl side by side; unplug the UART and see link-lost within 3 s.

**Safety notes.** Read-only phase: no arming or mode commands yet except in SITL.

## Phase 2: Laptop web dashboard

**Goal.** A browser dashboard served by the Pi (through the tunnel) that shows the aircraft and gives basic commands.

**Fun demo.** On your laptop away from home, see an artificial horizon, a map with the quad's position, battery gauge and mode; in SITL, click Arm, Takeoff to 10 m, then RTL and watch it come home on the map.

**Tasks.**

1. **Backend.** FastAPI app with a WebSocket that streams vehicle state at 10 Hz and a REST API for commands.
2. **Auth.** Rely on Cloudflare Access for the outer gate, and add a local session login on the app itself for command endpoints.
3. **Telemetry UI.** Attitude indicator, map (Leaflet + OpenStreetMap), battery, GPS, link health and a message log.
4. **Command UI.** Arm/disarm, mode select (STABILIZE, ALT\_HOLD, LOITER, GUIDED, RTL, LAND), takeoff, RTL and land buttons.
5. **Confirm step.** Every command that can spin motors (arm, takeoff, mode to GUIDED or AUTO) needs a slide-to-confirm and shows current state before it sends.
6. **Remote latency banner.** Show round-trip time to the Pi so you know how stale the view is over the tunnel.

**Test in SITL.** Full arm, takeoff, land and RTL loop from the browser against SITL; Playwright test that clicks through it in CI.

**Test on hardware.** Props off: arm and disarm from the dashboard while someone is at home with the RC transmitter in hand. First real takeoff from the dashboard only after Phase 5.

**Safety notes.** Arming from the dashboard requires login plus the confirm step. Until Phase 5, real-aircraft arming from the dashboard is blocked by a config flag that is off by default.

## Phase 3: Camera

**Goal.** Live video from the Pi Camera inside the dashboard, plus photos tagged with position.

**Fun demo.** Carry the quad around the yard with the dashboard open and see live video next to the moving map; snap a photo and see it pinned on the map where it was taken.

**Tasks.**

1. **Capture service.** `picamera2` pipeline with a low-latency stream and a full-resolution still path.
2. **Streaming.** WebRTC for low latency on the LAN; MJPEG fallback that works through the tunnel **(guess: WebRTC through Cloudflare may need a TURN server)**.
3. **Dashboard video panel.** Video with a telemetry overlay (altitude, speed, heading, battery).
4. **Photo capture.** Button and API that saves a still with GPS, altitude and attitude in EXIF and a JSON sidecar; gallery page.
5. **Recording.** Start/stop video recording to the SD card with a size cap.
6. **Camera trigger hook.** Expose "take photo" as an action the peripheral and mission systems will reuse later.

**Test in SITL.** Use a fake camera source (test pattern) so the dashboard and photo tagging work against SITL position.

**Test on hardware.** Bench and hand-carried walk; measure stream latency on LAN and through the tunnel.

**Safety notes.** Watch CPU temperature on the Pi 5 while streaming; add a heatsink or fan **(guess: active cooler recommended)**. Video must never starve the MAVLink process, so run it as a separate service.

## Phase 4: Calibration wizard

**Goal.** Calibration that feels like a guided setup app: one step per screen, a picture of what to do, live feedback that shows it worked, and a clear Next button. It drives the same ArduPilot calibration commands underneath.

**Fun demo.** Hand the transmitter to someone who has never set up a drone. They finish radio calibration in under 3 minutes by following the screens, watching each stick light up on a picture of the transmitter.

**Design principles.**

- One action per screen, with an illustration of the transmitter or quad orientation.
- Live feedback before moving on: a bar moves, a check mark appears, or the step says what went wrong in plain words.
- Progress bar and the ability to go back or restart a single step.
- Results summary at the end, saved with a timestamp so you can compare later.
- Nothing writes parameters until the final review screen, and old values are backed up first.

**Tasks.**

1. **Wizard framework.** Reusable step engine in the dashboard (step, illustration, live widget, pass check, next) plus a backend that reads and writes params safely with backup and restore.
2. **Radio calibration.** Steps: pick transmitter mode (Mode 2 default), move each stick to its ends while a live picture fills in min and max, center sticks, detect and name switches by flipping them, check reversed channels, assign flight modes to the mode switch visually. Writes `RCx_MIN/MAX/TRIM` and `FLTMODEx`.
3. **ESC and motor setup.** Props-off checklist gate, ESC calibration guide, then motor test: a top-down picture of the quad highlights one motor, spins it at low throttle for 2 s, and asks "Did the front-right motor spin clockwise?" with Yes or No. A wrong answer explains the fix (swap two wires, or set `MOT_` order params).
4. **Accelerometer.** Six-orientation flow with a 3D quad picture showing which side goes down, live gravity vector, and auto-advance when the board is still.
5. **Compass.** Rotate the quad while a sphere fills in coverage; shows offsets and a pass or retry result.
6. **Level and final review.** Level trim, then a summary of every value that will change and a single Save.

**Test in SITL.** SITL supports RC and accel/compass calibration messages; drive the wizard end to end with simulated inputs and assert params are written and backed up.

**Test on hardware.** Do radio and accel on the bench first. Motor test last, props off, quad strapped down.

**Safety notes.** Motor test and ESC calibration require: props-off checkbox, login, slide-to-confirm, and a visible stop button that also works from the RC kill switch. Over the tunnel, motor tests are blocked unless a "someone is at the aircraft" toggle is set **(guess on the exact gate)**.

## Phase 5: Safety and failsafes

**Goal.** The aircraft does something safe in every failure we can think of, and you cannot take off with a known problem.

**Fun demo.** In SITL, start a mission, then pull the plug on the Pi process, then the laptop link, then drain the battery; the dashboard narrates each failsafe as the quad responds with LOITER, RTL or LAND.

**Tasks.**

1. **Pixhawk-side failsafes.** Configure and verify RC loss (`FS_THR_ENABLE`), battery (`BATT_FS_LOW_ACT`, `BATT_FS_CRT_ACT`), GCS loss (`FS_GCS_ENABLE`), EKF failsafe and geofence breach. These work even if the Pi is dead.
2. **Pi crash failsafe.** Pixhawk treats loss of the Pi's heartbeat in GUIDED or AUTO as GCS loss and switches to RTL or LOITER.
3. **Laptop link loss.** If the dashboard disconnects mid-command, the Pi holds position and then follows the configured action after a timeout.
4. **Preflight checks.** A checklist screen that must be green before arm: GPS fix and HDOP, EKF healthy, battery voltage, compass and accel calibrated, failsafes enabled, geofence loaded, props-on confirmation.
5. **Pi health watchdog.** CPU temperature, throttling, memory, SD card space and service restarts shown on the dashboard and logged.
6. **Unlock real-aircraft arming.** Flip the Phase 2 config flag only after this phase passes its tests.

**Test in SITL.** Scripted fault injection for each failsafe with expected mode change and timing.

**Test on hardware.** Props off for RC loss and battery tests on the bench. First hover outside with RC in hand, then trigger RC loss deliberately at height and confirm RTL.

**Safety notes.** Fly first hovers with someone on the transmitter, away from people, within visual line of sight and local rules.

## Phase 6: Peripherals

**Goal.** A plugin system for actuators and sensors on the Pi's GPIO or USB, configured and tested from the dashboard, triggered by a button or an RC switch. The egg dropper is the first plugin.

**Fun demo.** Hover over a target, flip an RC switch, and the egg drops. Also click Drop in the dashboard and watch it happen on the live video.

**Tasks.**

1. **Plugin interface.** Base class with `setup`, `actions` (named, with parameters), `state`, `teardown`, and a config schema; plugins discovered from a folder.
2. **Hardware drivers.** Hardware PWM on Pi 5 GPIO 12/13 or 18/19 for servos **(guess on pins; Pi 5 PWM differs from older Pis)**, plain GPIO, and USB serial devices.
3. **Dashboard peripherals page.** Add a plugin, pick a pin from a picture of the header, set positions with sliders while the servo moves live, save.
4. **Triggers.** Bind an action to a dashboard button, an RC channel and switch position (read from `RC_CHANNELS`), or a MAVLink command.
5. **Egg dropper plugin.** Release and reset positions, a hold time, a safety lock that blocks release while disarmed unless in test mode, and an event in the log with position.
6. **Mission hook.** Expose peripheral actions to the mission engine for Phase 7.

**Test in SITL.** Mock GPIO backend; RC switch events from SITL's RC override trigger the action; unit tests for the plugin loader.

**Test on hardware.** Bench test the servo with the Pi only, then on the quad with props off, then a low hover drop over grass.

**Safety notes.** Power the servo from a separate 5V supply with a common ground, not the Pi's 5V pin, to avoid brownouts. Keep the dropper's weight inside the quad's payload margin and recheck balance.

## Phase 7: Missions

**Goal.** Plan, upload and fly waypoint missions from the dashboard, with peripheral and camera actions at waypoints.

**Fun demo.** Draw a route on the map, mark waypoint 3 as "drop egg" and waypoint 5 as "take photo", press Start, and watch the quad fly it, drop the egg on target and come home.

**Tasks.**

1. **Mission editor.** Click to add waypoints on the map, set altitude and speed per leg, drag to reorder, show distance, time and battery estimate.
2. **Upload and download.** MAVLink mission protocol (`MISSION_COUNT`, `MISSION_ITEM_INT`, `MISSION_ACK`) with verification read-back.
3. **Run controls.** Start (switch to AUTO), pause (LOITER or BRAKE), resume, skip to waypoint, abort to RTL.
4. **Geofence.** Draw a polygon and max altitude on the map, upload as fence, show it live; required by preflight checks.
5. **Waypoint actions.** On reaching a waypoint (`MISSION_ITEM_REACHED`), the Pi runs the bound peripheral or camera action, then lets the mission continue **(guess: Pi-side actions rather than ArduPilot DO\_ commands, so any plugin works)**.
6. **Mission library.** Save, load and duplicate missions; flight history with the path flown and photos taken.

**Test in SITL.** Fly every saved mission in SITL before the real aircraft can load it; assert actions fire at the right waypoint; test pause, resume and abort.

**Test on hardware.** Short 3-waypoint mission in an open field with RC in hand, then add the egg drop.

**Safety notes.** Geofence and RTL altitude are mandatory before AUTO is allowed. Starting a mission remotely uses the same confirm step as arming.

## Phase 8: Pi sensors

**Goal.** Bring the extra IMU and the LiDAR online and put their data to work.

**Fun demo.** Fly a low pass over uneven ground and watch a live altitude-above-ground graph from the LiDAR, alongside the barometer altitude, in the dashboard.

**Tasks.**

1. **Sensor plugin type.** Extend the Phase 6 plugin system to sensors with rate, units and health.
2. **LiDAR integration.** Either send `DISTANCE_SENSOR` from the Pi to the Pixhawk as a rangefinder, or wire the LiDAR to the Pixhawk directly and read it back. **Open question below.**
3. **IMU driver and logging.** Read the standalone IMU on I2C or SPI at a fixed rate, timestamp it, and log alongside Pixhawk attitude. **Role open question below.**
4. **Dashboard graphs.** Live and recorded plots for any sensor.
5. **Terrain-following option.** Use rangefinder altitude for low-altitude missions once LiDAR is trusted.

**Test in SITL.** SITL has a simulated rangefinder; feed recorded IMU data through the plugin in tests.

**Test on hardware.** Bench readings against a tape measure; then a hover at fixed heights.

**Safety notes.** Do not let a new sensor feed the EKF until it has been compared against existing sensors on several flights.

## Phase 9: Full autonomy

**Goal.** The quad makes its own decisions in flight: lands precisely on a marker and avoids obstacles.

**Fun demo.** Fly a mission that ends with the quad finding an ArUco marker on the ground with the camera and landing on it within 20 cm **(guess on accuracy)**.

**Tasks.**

1. **Marker detection.** OpenCV ArUco or AprilTag detection on the Pi Camera stream.
2. **Precision landing.** Send `LANDING_TARGET` messages to ArduPilot's precision landing (`PLND_` params).
3. **Obstacle sensing.** Forward-facing distance from LiDAR or camera depth as `OBSTACLE_DISTANCE`; enable ArduPilot's simple avoidance.
4. **Guided behaviors.** Pi-driven GUIDED-mode behaviors such as search pattern, follow a moving target, or orbit a point, each as a mission action.
5. **Autonomy supervisor.** A state machine that can always fall back to LOITER or RTL and hands control to RC instantly.

**Test in SITL.** Gazebo or the SITL precision-landing simulation for marker landing; scripted obstacles for avoidance.

**Test on hardware.** Marker landing from 5 m first with RC in hand, then from a mission.

**Safety notes.** Autonomy never disables RC override or Pixhawk failsafes. Each behavior ships behind a dashboard toggle.

## Later: Phone control

Reuse the dashboard as a mobile-friendly web app or PWA first, with large touch controls and the same confirm steps. A native app only if the web version falls short.

## Safety rules for every phase

- Props off for any motor, ESC or arming work on the bench.
- RC transmitter always overrides; never ship code that blocks a mode switch from RC.
- Arming, motor tests, takeoff and mission start are gated behind Cloudflare Access, app login and a slide-to-confirm step. Remotely, motor-spinning actions also need the "someone is at the aircraft" toggle.
- Pixhawk failsafes stay enabled and are never delegated to the Pi alone.
- Every new feature flies in SITL before the real aircraft.
- Fly within visual line of sight, away from people, and within local drone rules.

## Open questions

- [ ] **Role of the standalone IMU.** Options: logging and vibration analysis only; cross-check against the Pixhawk's IMUs; or input to a Pi-side vision or navigation filter. My lean is logging and vibration analysis first **(guess)**.
- [ ] **LiDAR on the Pi or the Pixhawk.** On the Pixhawk is simpler and works if the Pi crashes; on the Pi lets one sensor serve both obstacle logic and altitude. My lean is Pixhawk for a downward altitude LiDAR, Pi for a forward obstacle LiDAR **(guess)**.
- [ ] **Exact Pixhawk board and firmware.** Confirm whether it is a 1 MB flash clone in Phase 0.
- [ ] **LiDAR and IMU models.** Needed to pick drivers and interfaces.
- [ ] **Frontend framework.** Svelte or plain TypeScript; either works, pick at the start of Phase 2.

## Guesses made in this plan

Stack choices (FastAPI, Svelte, WebRTC), link-loss thresholds, the Pi 5 UART overlay and PWM pins, the remote motor-test gate, Pi-side waypoint actions, the active cooler, and the 20 cm landing target are all my assumptions. Change any of them here and the later phases adjust.
