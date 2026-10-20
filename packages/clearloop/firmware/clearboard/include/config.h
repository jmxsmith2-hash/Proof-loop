// ClearBoard compile-time defaults. Everything marked (runtime) can be changed
// later over HTTP (POST /api/config) and is stored in flash.
#pragma once

// ---- pins (ESP32-S3 DevKitC-1; see hardware/clearboard/README.md) ----------
#define PIN_BENDER_IN1 4    // H-bridge channel A (bender actuator)
#define PIN_BENDER_IN2 5
#define PIN_DOOR_IN1 6      // H-bridge channel B (door opener)
#define PIN_DOOR_IN2 7
#define PIN_FAN 15          // low-side MOSFET for the 24 V cooling fan
#define PIN_BENDER_ISENSE 1 // ADC1: current-sense amplifier output, bender
#define PIN_DOOR_ISENSE 2   // ADC1: current-sense amplifier output, door
#define PIN_LIMIT_SW 16     // backup trigger switch to GND (internal pull-up)
#define PIN_ESTOP 17        // normally-closed e-stop to GND (fail-safe); jumper to GND if not fitted
#define PIN_LED 21          // status LED (active high)

// ---- motion defaults (runtime) ----------------------------------------------
#define DEF_FULL_TRAVEL_MS 6500   // time for a full stroke at 100 % duty
#define DEF_STALL_AMPS 2.6f       // above this for DEF_STALL_MS = stalled / obstacle
#define DEF_STALL_MS 250
#define DEF_END_AMPS 0.08f        // below this after start-up = internal end switch reached
#define DEF_AMPS_PER_VOLT 1.0f    // current-sense gain: amps per volt at the ADC pin
#define DEF_DOOR_TRAVEL_MS 4000
#define DEF_DOOR_STALL_AMPS 1.2f  // door obstacle detection is deliberately sensitive

// ---- behaviour (runtime) ------------------------------------------------------
#define DEF_MARKER_BASE 9000       // ClearLoop trigger protocol: M73 L<base + code>
#define DEF_CYCLE_LOCKOUT_MS 60000 // ignore a second trigger for the same loop
#define DEF_FAN_SECONDS 0          // run the fan this long at DOOR_OPEN (0 = off)

#define FW_VERSION "0.1.0"
