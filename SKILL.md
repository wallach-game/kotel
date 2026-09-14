# Velxio Boiler Chip — Dev Notes

## What's here

- `velxio-chip.h` — the **stock**, unmodified Velxio custom-chip API
  (byte-identical to `backend/sdk/velxio-chip.h` upstream). Do not add
  macros, types, or typedefs to this file — chip code must work against
  the real API, nothing invented locally.
- `chip.c` / `chip.json` — the "kotel" (boiler) chip source. This is the
  canonical dev copy; it must stay in sync with the same source embedded
  in `velxio-project (1).vlx` (`components[0].properties.sourceC` /
  `chipJson`, plus the `fileGroups` archive copy).
- `vlx_sync.py` — keeps `chip.c`/`chip.json` and the `.vlx` in sync so you
  never hand-edit the JSON blob:
  - `python3 vlx_sync.py unpack "velxio-project (1).vlx"` — pull the
    `.vlx`'s embedded source out into `chip.c`/`chip.json` (e.g. after
    re-exporting from the app).
  - `python3 vlx_sync.py pack "velxio-project (1).vlx"` — push edited
    `chip.c`/`chip.json` back into the `.vlx` (both the live component
    and the `fileGroups` archive copy), ready to re-import into the app.
  - `python3 vlx_sync.py check "velxio-project (1).vlx"` — verify they
    match; exits 1 if not. `test_automation.sh` step 1 runs this.
- `check_structs.c` — compiles `velxio-chip.h` alone; if it builds, the
  header's own `_Static_assert`s confirm the I2C/UART/SPI config struct
  ABI hasn't drifted.
- `chip_harness.py` — shared compile + wasmtime host-environment harness
  (WASI stdio, `vx_*` imports, settable digital pins via `digital_in`,
  settable attribute overrides via `attr_overrides`). Both test files below
  import this instead of duplicating it.
- `test_chip_runtime.py` — basic runtime smoke test: instantiates the
  `.wasm` with `wasmtime`, calls `chip_setup()`, fires the timer callback
  across several simulated ticks, and asserts on real output (pins
  registered, timer armed, `target_temp` computed correctly, display buffer
  written). Not printf theater.
- `boiler_log.py` — parses the chip's per-tick log line into rows, and the
  two required measurement helpers: `measured_startup_delay_s()` and
  `measured_cycle_period()`.
- `test_boiler_timing.py` — acceptance test for the ignition-delay and
  cycling behaviours (see below): runs the chip for 10 simulated minutes,
  measures cold and warm vent-open/heat-start delays and the steady-state
  cycle period from the log, and checks all of them against tolerance
  (real-measured targets: 36s/42s cold, 22s/27s warm, 71s period). Also
  checks the mid-delay abort path.
- `test_automation.sh` — runs all of the above inside the `velxio`
  container (`docker compose up -d` first), since that's where the
  wasi-sdk toolchain and the `wasmtime` Python package live.

## What the boiler chip does

Pins: `ON/OFF` (digital in — the demand/call-for-heat signal), `12V REF`
(analog out, driven to 5.0V), `TEMP 0-12V` (analog in — the setpoint dial),
`GND`.

Attributes (live-tunable via the diagram editor's part inspector, no
recompile) — cold-start and warm-restart timings are real measurements
off the actual app, not made up:
- `ignition_delay_s` (42), `vent_delay_s` (36) — cold start: demand rises
  → vent opens at 36s → heat starts at 42s.
- `reignition_delay_s` (27), `reignition_vent_delay_s` (22) — warm
  restart mid-cycling: hysteresis calls for heat again → vent opens at
  22s → heat resumes at 27s. Shorter than cold start — the vent
  mechanism hasn't fully reset.
- `cycle_period_s` (71) — reference/documentation only, see point 3 below.
- `sensor_tau_s` (16), `hysteresis_k` (2) — drive the emergent cycling.
- `min_temp` (45), `max_temp` (80), `max_voltage` (12) — the `TEMP 0-12V`
  → setpoint mapping.

Every simulated second, `boiler_tick()`:

1. Reads `TEMP 0-12V`'s voltage and maps it to the setpoint (`target_temp`):
   `min_temp` at 0V to `max_temp` at `max_voltage` (linear, clamped).
2. Reads `ON/OFF` as the demand signal and runs a 3-state state machine —
   `OFF` → `STARTING` → `HEATING`:
   - `OFF`→`STARTING` on demand's rising edge (a **cold** start).
   - `STARTING`: no heat is produced. `vent_open` flips true at
     `vent_delay_s`/`reignition_vent_delay_s` (cold vs. warm — see
     below); the burner actually lights at `ignition_delay_s`/
     `reignition_delay_s`, moving to `HEATING`. If demand drops during
     this window, aborts straight back to `OFF` with no heat produced.
   - `HEATING`: stays here until demand drops (→ `OFF`, burner + vent
     off). While here the burner cycles on/off around the setpoint
     (point 3) — it does **not** hold steady once at temperature. Each
     time the hysteresis controller calls for heat again mid-session,
     state drops back to `STARTING` for a **warm** restart (shorter
     timing than the initial cold one) before actually relighting.
3. Cycling is emergent, not a scheduled timer: an internal `sensor_temp`
   chases `water_temp ± 2°C` (fixed swing, burner-driven — see
   `SENSOR_SWING_C`) with time constant `sensor_tau_s`, and a hysteresis
   band of width `hysteresis_k` around `target_temp` decides when the
   burner should turn off / call for reignition. Tracking `water_temp`
   (not `target_temp`) is what gives the loop real negative feedback: once
   the actual water is near the setpoint the sensor swings past the
   thresholds and the burner backs off, instead of cycling at a fixed
   duty cycle regardless of how hot the water has actually gotten (a real
   bug this model had — heating rate (+0.1°C/tick) exceeds cooling rate
   (-0.02°C/tick), so a decoupled sensor let `water_temp` run away
   unbounded past the setpoint with no ceiling). This is the same topology
   as an RC-relaxation oscillator (a 555 astable): a fast internal sensor
   lagging behind a slow bulk `water_temp` is exactly what makes a real
   boiler short-cycle. At the defaults (plus the fixed 27s warm
   reignition delay baked into every cycle) this settles to a ~71s
   steady-state period (target 71s ±10%), oscillating in a band a couple
   degrees above the literal setpoint (the same "differential" a real
   boiler has) — tune `sensor_tau_s`/`hysteresis_k` to shift it;
   `cycle_period_s` itself is not wired into the math, it's just the
   documented target those two are tuned against.
4. Heats `water_temp` toward the setpoint at +0.1°C/tick while the burner
   is on, or lets it drift down -0.02°C/tick toward a 20°C floor otherwise.
   Starts at 40°C (not a cold 20°C) so manual testing in the real app —
   which runs in wall-clock time — doesn't take forever to get anywhere
   near the 45-80°C setpoint range.
5. Logs one line per tick — `t, demand, burner_on, sensor_temp,
   water_temp, state, vent_open` (plus `target`) — flushed explicitly
   (see gotcha below), and draws a 64×64 display: a bar showing current
   water temp (red while the burner's on, blue when idle), a white line
   marking the target, and an `M:<state 0/1/2> S:<target> T:<water>`
   stats line rendered with a hand-rolled 3x5 bitmap font (the real API
   only gives raw RGBA pixel writes — no text primitive).

## Gotchas hit while building this (now covered by tests)

- **Handle `0` is a valid resource handle, not "creation failed".**
  `vx_timer_create()` (and pins, attrs, etc.) return 0-based indices, so
  the *first* one created is `0` — falsy in C. Don't write
  `if (!handle) { ...fail... }` against these; there's no documented
  sentinel failure value for them at all.
- **`printf()` in a chip only reliably flushes its first call.** These
  chips compile as WASI *reactor* modules (`--no-entry`, no `_start`),
  so nothing ever triggers libc's exit-time stdio flush. musl's first
  stdio write goes straight through unbuffered (before the internal
  buffer is allocated); every write after that sits buffered forever.
  Call `fflush(stdout)` after any `printf()` you want to actually see —
  or use `vx_log()`, which isn't buffered at all.
- **The test harness's `vx_attr_read` used to always return `0.0`,
  ignoring the registered default.** Harmless while nothing used
  attributes; would have silently broken every timing default (e.g.
  `ignition_delay_s` reading as `0`) the moment `chip.c` started using
  them. Fixed in `chip_harness.py` to return the registered default (or
  `attr_overrides[name]` if set) — check this first if a chip using
  attributes behaves oddly under test but fine in the real app.

## Running the tests

```bash
docker compose up -d
./test_automation.sh
```
