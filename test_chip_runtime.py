#!/usr/bin/env python3
"""Functional test for chip.c against the stock velxio-chip.h.

Compiles chip.c with the real wasi-sdk toolchain (same flags the Velxio
backend's chip_compile.py uses), loads the resulting .wasm with wasmtime,
and actually runs chip_setup() plus several simulated timer ticks —
asserting on the real behavior, not just "does it print a checkmark".

Requires: wasi-sdk (WASI_SDK env var or /opt/wasi-sdk) and the `wasmtime`
Python package. Both are present in the velxio-wasi:master container this
project's docker-compose.yaml runs — see test_automation.sh.
"""
from __future__ import annotations

import sys

from chip_harness import ChipHarness, compile_chip


def main() -> None:
    wasm = compile_chip()
    print(f"compiled OK ({len(wasm)} bytes)")

    failures = []

    def check(name: str, cond: bool, detail: str = ""):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    # 15V fed in simulates a reading past the pot's real 12V ceiling ->
    # must clamp to 12V -> target = 35 + 12*3.75 = 80.0C (not higher).
    chip = ChipHarness(wasm, temp_voltage=15.0)
    chip.digital_in["ON/OFF"] = 1  # demand on from the start, so heating actually runs
    chip.run_setup()

    check("registers exactly 3 pins", len(chip.pins) == 3, f"got {len(chip.pins)}")
    check("pin 0 is ON/OFF", chip.pins[0]["name"] == "ON/OFF" if chip.pins else False)
    check("pin 2 is TEMP 0-12V", chip.pins[2]["name"] == "TEMP 0-12V" if len(chip.pins) > 2 else False)
    check("creates exactly 1 timer", len(chip.timers) == 1, f"got {len(chip.timers)}")
    check("timer is active after setup", chip.timers[0]["active"] if chip.timers else False,
          "vx_timer_start never ran — check the handle-0 falsy-check trap")
    check('logs "Kotel: initialized"', "Kotel: initialized" in chip.log_lines)
    check("declares a 64x64 display", (chip.fb_w, chip.fb_h) == (64, 64), f"got {chip.fb_w}x{chip.fb_h}")

    # Past ignition_delay_s (default 30) so the burner is actually running
    # by the time we check target/water/heating below.
    for _ in range(35):
        chip.tick()

    check("printf output survives past tick 1 (fflush)", len(chip.stdout_lines) == 35,
          f"got {len(chip.stdout_lines)} lines — stdio buffering regression")
    if chip.stdout_lines:
        last = chip.stdout_lines[-1]
        check("target clamps to 80.0C at/above the 12V ceiling", "target=80.0C" in last, last)
        check("burner_on=1 once past ignition delay", "burner_on=1" in last, last)

    # 6V, well inside the 0-12V range -> target = 35 + 6*3.75 = 57.5C,
    # confirming the response is actually proportional to voltage, not just clamped.
    chip2 = ChipHarness(wasm, temp_voltage=6.0)
    chip2.digital_in["ON/OFF"] = 1
    chip2.run_setup()
    chip2.tick()
    if chip2.stdout_lines:
        check("target=57.5C from 6V input (proportional, not clamped)",
              "target=57.5C" in chip2.stdout_lines[-1], chip2.stdout_lines[-1])

    check("display buffer is non-blank after ticking", any(chip.fb_pixels), "framebuffer never written")

    # Stats text ("M:.. S:.. T:..") is drawn white-on-black in the top-left corner.
    top_rows = chip.fb_pixels[:chip.fb_w * 6 * 4]
    white_px = sum(
        1 for i in range(0, len(top_rows), 4)
        if tuple(top_rows[i:i + 3]) == (0xFF, 0xFF, 0xFF)
    )
    check("stats text rendered (white pixels in top rows)", white_px > 20, f"got {white_px} white pixels")

    print(f"\n{len(failures)} failing" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
