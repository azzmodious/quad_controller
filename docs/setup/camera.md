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

## Troubleshooting

- **"Pipeline handler in use by another process"**: something else holds the
  camera (an `rpicam-*` command still running). `pkill -f rpicam` and retry.
- **"cannot listen on 127.0.0.1:8000"**: another process is on port 8000.
  `ss -ltnp | grep 8000` shows which.
- **Page loads but the picture stutters**: try the Low bandwidth button, or
  run with `--width 640 --height 480 --fps 10`.
