# ClearBoard: electronics reference design

ClearBoard is an open ejector controller. It's built from off-the-shelf modules,
needs no custom PCB, and can be assembled on a perfboard or a small carrier
board. A KiCad carrier board is a welcome contribution. This document is the
reference that the firmware pin map (`firmware/clearboard/include/config.h`)
follows.

> **Scope:** electronics only. Mechanical parts (bender bracket, actuator
> mounts, door-arm linkage) depend on the printer model and are **not
> included**. Use your existing kit's mechanics or contribute open designs.

## Block diagram

```
 24 V PSU ─┬─▶ buck 24→5 V ─▶ ESP32-S3 DevKitC-1 (5V pin)
           │
           ├─▶ DRV8871 #1 ──▶ bender linear actuator      shunt + INA181 ─▶ GPIO1 (ADC)
           ├─▶ DRV8871 #2 ──▶ door actuator (optional)    shunt + INA181 ─▶ GPIO2 (ADC)
           └─▶ AO3400 low-side MOSFET ──▶ 24 V fan (optional)

 GPIO16 ◀── limit switch (NO, to GND)      backup trigger, pressed by the bed at bend_z
 GPIO17 ◀── e-stop (NC, to GND)            fail-safe: pressing it or a broken wire stops motion
 GPIO21 ──▶ status LED (+ 330 Ω)
```

## Bill of materials

| Qty | Part | Notes |
|---|---|---|
| 1 | ESP32-S3-DevKitC-1 (N8R2 or similar) | Any ESP32 works. Keep current sensing on ADC1 pins (ADC2 doesn't work while Wi-Fi is on). |
| 1–2 | DRV8871 H-bridge breakout | 6.5–45 V, 3.6 A peak. Set ILIM to about 1.5× actuator running current as a hardware limit. |
| 1–2 | 12 or 24 V linear actuator with **internal limit switches**, 50 mm stroke, ≥ 64 N | The firmware detects end of travel by current dropping to zero at the internal switch. |
| 1–2 | 50 mΩ 1 W shunt + INA181A2 (gain 50) or INA180A1 (gain 20) module | Gain 20 × 50 mΩ = 1 V/A, which matches the default `amps_per_volt = 1.0`. Keep the output under 3.1 V. |
| 1 | MP1584 / LM2596 buck module, 24 V → 5 V | Powers the ESP32 |
| 1 | AO3400 N-MOSFET + 1N5819 flyback diode + 10 kΩ gate pull-down | Fan switch (optional) |
| 1 | 24 V, ≥ 3 A power supply | External supply. Don't draw actuator current from the printer. |
| 1 | Micro limit switch (NO) | Backup trigger (optional but recommended) |
| 1 | NC mushroom e-stop | Or a jumper from GPIO17 to GND if not fitted |
| 1 | LED + 330 Ω | Status |
| — | JST-XH or screw terminals, ferrules, 22 AWG signal / 18 AWG motor wire | Strain-relieve every cable that moves with the bed or door |

## Wiring

| ESP32 pin | Connects to |
|---|---|
| GPIO4 / GPIO5 | DRV8871 #1 IN1 / IN2 (bender) |
| GPIO6 / GPIO7 | DRV8871 #2 IN1 / IN2 (door) |
| GPIO1 | INA output, bender shunt |
| GPIO2 | INA output, door shunt |
| GPIO15 | MOSFET gate (fan) |
| GPIO16 | Limit switch to GND |
| GPIO17 | E-stop NC contact to GND |
| GPIO21 | LED anode (through 330 Ω) |
| 5V / GND | Buck output. Tie all grounds together at one point near the PSU. |

Put the shunt in the motor supply line (high side, between 24 V and DRV8871
VM), or low side between DRV8871 GND and supply GND. Use whichever your INA
module supports.

## Calibration (do once per actuator)

1. Flash the firmware and join Wi-Fi: connect to the `ClearBoard-xxxx` access
   point (password `clearboard`), then `POST /api/config` with `ssid` and
   `password`, then `POST /api/restart`.
2. With nothing attached to the actuator, press **Test bend** on the board page
   and read `bender_amps` and `last.peak_current_a` from `/api/status`.
3. Set `stall_amps` about 30 % above the peak seen during a normal bend
   **with a plate fitted**, and `end_amps` just above the idle reading.
4. Time a full stroke and set `full_travel_ms`.
5. For the door, set `door_stall_amps` low enough that a hand in the way stops
   it. Test this before running unattended.

## Safety notes

- The firmware cuts motor drive within about 5 ms of e-stop, stall or obstacle
  detection, but **the DRV8871 ILIM resistor is the real current limit**. Always
  fit it.
- Keep hands out of the door path. Door obstacle detection is a convenience, not
  a certified safety function.
- Fuse the 24 V supply output, for example with a 5 A blade fuse.
