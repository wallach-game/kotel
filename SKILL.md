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
  two measurement helpers the timing acceptance test uses:
  `measured_startup_delay_s()` and `measured_cycle_period()`.
- `test_boiler_timing.py` — acceptance test for the ignition-delay and
  cycling behaviours (see below): runs the chip for 10 simulated minutes,
  measures the real startup delay and steady-state cycle period from the
  log, and checks both against tolerance. Also checks the mid-delay abort
  path.
- `test_automation.sh` — runs all of the above inside the `velxio`
  container (`docker compose up -d` first), since that's where the
  wasi-sdk toolchain and the `wasmtime` Python package live.

## What the boiler chip does

Pins: `ON/OFF` (digital in — the demand/call-for-heat signal), `12V REF`
(analog out, driven to 5.0V), `TEMP 0-12V` (analog in — the setpoint dial),
`GND`.

Attributes (live-tunable via the diagram editor's part inspector, no
recompile): `ignition_delay_s` (default 30), `cycle_period_s` (default
120, reference/documentation only — see below), `sensor_tau_s` (default
30), `hysteresis_k` (default 3).

Every simulated second, `boiler_tick()`:

1. Reads `TEMP 0-12V`'s voltage and maps it to the setpoint (`target_temp`):
   0V → 35°C, 12V → 80°C (linear, clamped).
2. Reads `ON/OFF` as the demand signal and runs a 3-state state machine —
   `OFF` → `STARTING` → `HEATING`:
   - `OFF`→`STARTING` on demand's rising edge.
   - `STARTING`: produces no heat for `ignition_delay_s`. If demand drops
     during this window, aborts straight back to `OFF` with no heat ever
     produced. Otherwise, after the delay, moves to `HEATING` and starts
     the burner.
   - `HEATING`: stays here until demand drops (→ `OFF`, burner stops).
     While here, the burner cycles on/off around the setpoint (below) —
     it does **not** hold steady once at temperature.
3. Cycling is emergent, not a scheduled timer: an internal `sensor_temp`
   chases a burner-driven reference (`target_temp ± 2°C`, fixed swing —
   see `SENSOR_SWING_C`) with time constant `sensor_tau_s`, and a
   hysteresis band of width `hysteresis_k` around `target_temp` toggles
   the burner off/on as `sensor_temp` crosses it. This is the same
   topology as an RC-relaxation oscillator (a 555 astable): a fast
   internal sensor lagging behind a slow bulk `water_temp` is exactly
   what makes a real boiler short-cycle. At the defaults this settles to
   a ~116s steady-state period (target 120s ±10%) — tune `sensor_tau_s`/
   `hysteresis_k` to shift it; `cycle_period_s` itself is not wired into
   the math, it's just the documented target those two are tuned against.
4. Heats `water_temp` toward the setpoint at +0.1°C/tick while the burner
   is on, or lets it drift down -0.02°C/tick toward 20°C floor otherwise.
5. Logs one line per tick — `t, demand, burner_on, sensor_temp,
   water_temp, state` (plus `target`) — flushed explicitly (see gotcha
   below), and draws a 64×64 display: a bar showing current water temp
   (red while the burner's on, blue when idle), a white line marking the
   target, and an `M:<state 0/1/2> S:<target> T:<water>` stats line
   rendered with a hand-rolled 3x5 bitmap font (the real API only gives
   raw RGBA pixel writes — no text primitive).

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
