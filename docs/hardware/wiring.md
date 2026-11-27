# Wiring the Pi 5 to the Pixhawk 2.4.8

The Pi talks to the Pixhawk over a 3.3 V serial link on the Pixhawk's **TELEM2** port. Only three wires join them: TX, RX and ground. Power stays separate.

> Props off for all of Phase 0. Nothing in this phase arms the motors, but there is no reason to have props on the bench.

## What you need

- A 6-pin DF13 cable for TELEM2 (most 2.4.8 kits include one), or a DF13 pigtail
- 3 female Dupont jumpers for the Pi header
- A separate **5 V, 5 A** BEC (or UBEC) to power the Pi from the flight battery
- Optional: a multimeter to confirm voltages before plugging in

## Pinout

Pixhawk 2.4.8 **TELEM2** (DF13, pin 1 is the red wire on most cables):

| Pin | Signal | Connect to |
| --- | --- | --- |
| 1 | +5 V out | **Nothing.** Leave it unconnected. |
| 2 | TX (out) | Pi **pin 10** (GPIO15, RXD) |
| 3 | RX (in) | Pi **pin 8** (GPIO14, TXD) |
| 4 | CTS | Nothing |
| 5 | RTS | Nothing |
| 6 | GND | Pi **pin 6** (GND) |

Raspberry Pi 5 header, the pins we use:

```
          3V3  (1) (2)  5V
               (3) (4)  5V
               (5) (6)  GND   <-- Pixhawk TELEM2 pin 6 (GND)
               (7) (8)  GPIO14 TXD  --> Pixhawk TELEM2 pin 3 (RX)
          GND  (9) (10) GPIO15 RXD  <-- Pixhawk TELEM2 pin 2 (TX)
```

TX always goes to RX. If you get no heartbeat later, swapping pins 8 and 10 is the first thing to try; it cannot damage anything at 3.3 V.

## Power

- Power the Pi 5 from its **own 5 V 5 A BEC**, not from the Pixhawk's TELEM 5 V pin. The Pixhawk rail cannot supply a Pi 5 and a brownout there can reset the autopilot in flight.
- Easiest is a BEC with a USB-C lead into the Pi's USB-C port.
- If you feed 5 V into header pins 2/4 instead, add `usb_max_current_enable=1` to `/boot/firmware/config.txt`, or the Pi limits USB current and warns about the supply.
- The Pixhawk keeps its usual power module. The shared ground through TELEM2 pin 6 is the only ground link you need for the serial line.

## Checklist

- [ ] Props are off.
- [ ] TELEM2 pin 1 (5 V) is not connected to anything.
- [ ] Pixhawk TX (pin 2) goes to Pi pin 10; Pixhawk RX (pin 3) goes to Pi pin 8.
- [ ] Pixhawk GND (pin 6) goes to Pi pin 6.
- [ ] The Pi is powered by its own 5 V BEC or USB-C supply.
- [ ] Wires are strain-relieved and cannot reach the props.
- [ ] Take a photo of the wiring and keep it with your notes.

Next: [Pixhawk params](pixhawk-params.md), then [Pi setup](../setup/pi-setup.md).
