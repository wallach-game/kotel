#!/usr/bin/env python3
"""Acceptance test for the boiler-upgrade timing behaviours:
  1. Ignition delay — no heat for ignition_delay_s after demand rises;
     aborts with no heat if demand drops mid-delay.
  2. Cycling around setpoint — emergent on/off oscillation near cycle_period_s,
     from sensor_tau_s + hysteresis_k, not a hardcoded timer.

Requires: wasi-sdk + wasmtime, same as test_chip_runtime.py — run via
test_automation.sh inside the velxio container.
"""
from __future__ import annotations

import sys

from boiler_log import measured_cycle_period, measured_startup_delay_s, parse_log
from chip_harness import ChipHarness, compile_chip

IGNITION_DELAY_S = 30.0
CYCLE_PERIOD_S = 120.0
DELAY_TOLERANCE_S = 1.0
PERIOD_TOLERANCE_FRAC = 0.10  # +-10%


def check(failures: list[str], name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run_for(chip: ChipHarness, ticks: int) -> None:
    for _ in range(ticks):
        chip.tick()


def main() -> None:
    wasm = compile_chip()
    print(f"compiled OK ({len(wasm)} bytes)")
    failures: list[str] = []

    # ── Acceptance: startup delay + steady-state cycle period ──────────────
    print("\n=== ignition delay + cycling (demand held high) ===")
    chip = ChipHarness(wasm, temp_voltage=8.0)  # target = 35 + 8*3.75 = 65C
    chip.digital_in["ON/OFF"] = 1
    chip.run_setup()
    run_for(chip, 600)  # 10 simulated minutes — several steady-state cycles

    rows = parse_log(chip.stdout_lines)
    check(failures, "log has rows", len(rows) == 600, f"got {len(rows)}")

    delay = measured_startup_delay_s(rows)
    print(f"measured startup delay: {delay} s")
    check(failures, f"startup delay = {IGNITION_DELAY_S}s +-{DELAY_TOLERANCE_S}s",
          delay is not None and abs(delay - IGNITION_DELAY_S) <= DELAY_TOLERANCE_S,
          f"got {delay}")

    period, amplitude = measured_cycle_period(rows)
    print(f"measured cycle period: {period} s, peak-to-peak amplitude: {amplitude:.2f} C")
    period_ok = period is not None and abs(period - CYCLE_PERIOD_S) <= CYCLE_PERIOD_S * PERIOD_TOLERANCE_FRAC
    check(failures, f"cycle period = {CYCLE_PERIOD_S}s +-{PERIOD_TOLERANCE_FRAC:.0%}", period_ok, f"got {period}")
    check(failures, "cycling has a measurable amplitude (not flat)", amplitude > 0.5, f"got {amplitude:.2f}C")

    states_seen = {r["state"] for r in rows}
    check(failures, "state reaches HEATING (2)", 2 in states_seen, f"states seen: {states_seen}")

    # ── Abort: demand drops mid-delay -> no heat ever produced ─────────────
    print("\n=== abort mid-delay ===")
    chip2 = ChipHarness(wasm, temp_voltage=8.0)
    chip2.digital_in["ON/OFF"] = 1
    chip2.run_setup()
    run_for(chip2, 10)          # well inside the 30s ignition delay
    chip2.digital_in["ON/OFF"] = 0
    run_for(chip2, 40)          # past where ignition would have completed

    rows2 = parse_log(chip2.stdout_lines)
    never_heated = all(r["burner_on"] == 0 for r in rows2)
    check(failures, "no heat produced when demand drops mid-delay", never_heated,
          f"burner_on went high at t={next((r['t'] for r in rows2 if r['burner_on']==1), None)}")
    check(failures, "state returns to OFF (0) after abort", rows2[-1]["state"] == 0 if rows2 else False,
          f"final state: {rows2[-1]['state'] if rows2 else 'n/a'}")

    print(f"\n{len(failures)} failing" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
