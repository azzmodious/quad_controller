# Pi Camera live view

A first step toward Phase 3: a live MJPEG view and snapshots in the browser,
served on `localhost:8000`, which the Cloudflare tunnel already publishes as
`drone01.<your domain>` behind Cloudflare Access. Everything here works over
browser SSH; nobody needs to be at the quad.

## 1. Check the camera

```bash
rpicam-hello --list-cameras          # should list a sensor, e.g. imx708 or imx219
rpicam-still -n -t 1000 -o ~/first.jpg && ls -lh ~/first.jpg
```

`-n` means no preview window, which matters over SSH. If no camera is listed,
check `dmesg | grep -i -E "imx|camera|unicam|rp1-cfe"` and the ribbon cable
(that part needs someone at the Pi).

## 2. Run the live view

```bash
cd ~/quad_controller && git pull
.venv/bin/python -m quad_controller.camera
```

Open `https://drone01.<your domain>/camera/` and sign in with the Access PIN.
Ctrl+C in the SSH tab stops it. Useful flags: `--width 640 --height 480`
(less bandwidth), `--fps 10`, `--vflip --hflip` (camera mounted upside down),
`--quality 60`.

The camera only runs while a page is open. It starts on the first viewer and
stops five seconds after the last one leaves.

## Colour

The easiest fix for a colour tint is on the page itself, in the **Colour** panel:

1. Point the camera at something white or light grey, click **Tap white to
   balance**, then tap that object in the picture. The red and blue gains are
   set so it comes out neutral. Tap again to fine-tune.
2. Or turn off **Automatic white balance** and drag **Red** and **Blue**. A blue
   picture needs more red or less blue.
3. Click **Save as default**. Settings are saved to
   `~/.config/quad_controller/camera.json` and used every time the camera
   starts. Command-line flags still win over the saved file.

Each change restarts the camera, so the picture pauses for about a second.

From the command line instead:

- `--awbgains RED,BLUE`, e.g. `--awbgains 1.6,1.4`: fixed gains.
- `--awb indoor` (or tungsten, daylight, ...): a white balance preset.
- `--noir`: colour profile for NoIR cameras (no infrared filter).

## Troubleshooting

- **"Pipeline handler in use by another process"**: something else holds the
  camera (an `rpicam-*` command still running). `pkill -f rpicam` and retry.
- **"cannot listen on 127.0.0.1:8000"**: another process is on port 8000.
  `ss -ltnp | grep 8000` shows which.
- **Page loads but the picture stutters**: try the Low bandwidth button, or
  run with `--width 640 --height 480 --fps 10`.
