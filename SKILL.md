# Velxio Boiler Chip — Dev Notes

## What's here

- `velxio-chip.h` — the **stock**, unmodified Velxio custom-chip API
  (byte-identical to `backend/sdk/velxio-chip.h` upstream). Do not add
  macros, types, or typedefs to this file — chip code must work against
  the real API, nothing invented locally.
- `chip.c` / `chip.json` — the "kotel" (boiler) chip source. This is the
  canonical dev copy; it must stay in sync with the same source embedded
  in `velxio-project (9).vlx` (`components[0].properties.sourceC` /
  `chipJson`, plus the `fileGroups` archive copy).
- `vlx_sync.py` — keeps `chip.c`/`chip.json` and the `.vlx` in sync so you
  never hand-edit the JSON blob:
  - `python3 vlx_sync.py unpack "velxio-project (9).vlx"` — pull the
    `.vlx`'s embedded source out into `chip.c`/`chip.json` (e.g. after
    re-exporting from the app).
  - `python3 vlx_sync.py pack "velxio-project (9).vlx"` — push edited
    `chip.c`/`chip.json` back into the `.vlx` (both the live component
    and the `fileGroups` archive copy), ready to re-import into the app.
  - `python3 vlx_sync.py check "velxio-project (9).vlx"` — verify they
    match; exits 1 if not. `test_automation.sh` step 1 runs this.
- `check_structs.c` — compiles `velxio-chip.h` alone; if it builds, the
  header's own `_Static_assert`s confirm the I2C/UART/SPI config struct
  ABI hasn't drifted.
- `test_chip_runtime.py` — actually runs the compiled chip: instantiates
  the `.wasm` with `wasmtime`, calls `chip_setup()`, fires the timer
  callback across several simulated ticks, and asserts on real output
  (pins registered, timer armed, `target_temp` computed correctly,
  `water_temp` progression, display buffer written). Not printf theater.
- `test_automation.sh` — runs all of the above inside the `velxio`
  container (`docker compose up -d` first), since that's where the
  wasi-sdk toolchain and the `wasmtime` Python package live.

## What the boiler chip does

Pins: `ON/OFF` (digital in), `12V REF` (analog out, driven to 5.0V),
`TEMP 0-12V` (analog in), `GND`.

Every simulated second, `boiler_tick()`:
1. Reads `TEMP 0-12V`'s voltage and maps it to `target_temp`: 0V → 35°C,
   12V → 80°C (linear, clamped).
2. Heats `water_temp` toward `target_temp` at +0.1°C/tick, or lets it
   drift down -0.02°C/tick toward 20°C when not heating.
3. Logs the state via `printf` (flushed explicitly — see gotcha below)
   and draws it to a 64×64 display: a bar showing current water temp
   (red while heating, blue when idle), a white line marking the
   target, and an `E:<heating> S:<target> T:<water>` stats line
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

## Running the tests

```bash
docker compose up -d
./test_automation.sh
```
