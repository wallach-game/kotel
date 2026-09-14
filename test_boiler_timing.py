#!/usr/bin/env python3
"""Acceptance test for the boiler-upgrade timing behaviours:
  1. Ignition delay — no heat for ignition_delay_s (cold) / reignition_delay_s
     (warm restart mid-cycling) after demand/reignition triggers; aborts with
     no heat if demand drops mid-delay. vent_open leads heat by the matching
     vent_delay_s / reignition_vent_delay_s.
  2. Cycling around setpoint — emergent on/off oscillation near cycle_period_s,
     from sensor_tau_s + hysteresis_k, not a hardcoded timer.
  3. Regression: a live setpoint change while sensor_temp is frozen mid-cycle
     must not cause the burner to instantly flicker back off the same tick
     it ignites (see the test below for how this was actually found).

Requires: wasi-sdk + wasmtime, same as test_chip_runtime.py — run via
test_automation.sh inside the velxio container.
"""
from __future__ import annotations

import sys

from boiler_log import measured_cycle_period, measured_startup_delay_s, parse_log
from chip_harness import ChipHarness, compile_chip

# Real-measured reference values (from the actual app): cold start opens
# the vent at 36s and starts heating at 42s; a warm mid-cycle restart opens
# the vent at 22s and resumes heating at 27s; full cycle (heat-start to
# heat-start) is ~71s.
IGNITION_DELAY_S = 42.0
VENT_DELAY_S = 36.0
REIGNITION_DELAY_S = 27.0
REIGNITION_VENT_DELAY_S = 22.0
CYCLE_PERIOD_S = 71.0
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


def _rising_edges(rows: list[dict], key: str) -> list[int]:
    edges, prev = [], 0
    for r in rows:
        if r[key] == 1 and prev == 0:
            edges.append(r["t"])
        prev = r[key]
    return edges


def _falling_edges(rows: list[dict], key: str) -> list[int]:
    edges, prev = [], 0
    for r in rows:
        if r[key] == 0 and prev == 1:
            edges.append(r["t"])
        prev = r[key]
    return edges


def main() -> None:
    wasm = compile_chip()
    print(f"compiled OK ({len(wasm)} bytes)")
    failures: list[str] = []

    # ── Acceptance: startup delay + steady-state cycle period ──────────────
    print("\n=== ignition delay + cycling (demand held high) ===")
    chip = ChipHarness(wasm, temp_voltage=8.0)  # target = 45 + 8*(35/12) = 68.3C
    chip.digital_in["ON/OFF"] = 1
    chip.run_setup()
    run_for(chip, 600)  # 10 simulated minutes — several steady-state cycles

    rows = parse_log(chip.stdout_lines)
    check(failures, "log has rows", len(rows) == 600, f"got {len(rows)}")

    delay = measured_startup_delay_s(rows)
    print(f"measured startup delay: {delay} s")
    check(failures, f"cold ignition delay = {IGNITION_DELAY_S}s +-{DELAY_TOLERANCE_S}s",
          delay is not None and abs(delay - IGNITION_DELAY_S) <= DELAY_TOLERANCE_S,
          f"got {delay}")

    # Cold vent-open: demand rising edge -> first vent_open=1.
    demand_edges = _rising_edges(rows, "demand")
    vent_edges = _rising_edges(rows, "vent_open")
    cold_vent_delay = (vent_edges[0] - demand_edges[0]) if demand_edges and vent_edges else None
    print(f"measured cold vent-open delay: {cold_vent_delay} s")
    check(failures, f"cold vent-open delay = {VENT_DELAY_S}s +-{DELAY_TOLERANCE_S}s",
          cold_vent_delay is not None and abs(cold_vent_delay - VENT_DELAY_S) <= DELAY_TOLERANCE_S,
          f"got {cold_vent_delay}")

    # Warm restart: a vent_open falling edge marks the moment mid-cycle
    # reignition begins (vent_open resets to 0 the instant the hysteresis
    # controller calls for heat again) — measure both delays from there.
    vent_falls = _falling_edges(rows, "vent_open")
    if vent_falls:
        restart_t = vent_falls[0]
        warm_vent_t = next((r["t"] for r in rows if r["t"] >= restart_t and r["vent_open"] == 1), None)
        warm_heat_t = next((r["t"] for r in rows if r["t"] >= restart_t and r["burner_on"] == 1), None)
        warm_vent_delay = (warm_vent_t - restart_t) if warm_vent_t is not None else None
        warm_heat_delay = (warm_heat_t - restart_t) if warm_heat_t is not None else None
    else:
        warm_vent_delay = warm_heat_delay = None
    print(f"measured warm vent-open delay: {warm_vent_delay} s, warm reignition delay: {warm_heat_delay} s")
    check(failures, f"warm vent-open delay = {REIGNITION_VENT_DELAY_S}s +-{DELAY_TOLERANCE_S}s",
          warm_vent_delay is not None and abs(warm_vent_delay - REIGNITION_VENT_DELAY_S) <= DELAY_TOLERANCE_S,
          f"got {warm_vent_delay}")
    check(failures, f"warm reignition delay = {REIGNITION_DELAY_S}s +-{DELAY_TOLERANCE_S}s",
          warm_heat_delay is not None and abs(warm_heat_delay - REIGNITION_DELAY_S) <= DELAY_TOLERANCE_S,
          f"got {warm_heat_delay}")

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
    run_for(chip2, 10)          # well inside the 42s cold ignition delay
    chip2.digital_in["ON/OFF"] = 0
    run_for(chip2, 50)          # past where ignition would have completed

    rows2 = parse_log(chip2.stdout_lines)
    never_heated = all(r["burner_on"] == 0 for r in rows2)
    check(failures, "no heat produced when demand drops mid-delay", never_heated,
          f"burner_on went high at t={next((r['t'] for r in rows2 if r['burner_on']==1), None)}")
    check(failures, "state returns to OFF (0) after abort", rows2[-1]["state"] == 0 if rows2 else False,
          f"final state: {rows2[-1]['state'] if rows2 else 'n/a'}")

    # ── Regression: setpoint dropped while sensor_temp was frozen mid-cycle ─
    # sensor_temp is frozen for the whole STARTING window. If the user turns
    # the TEMP dial down during that freeze, the setpoint the chip computes
    # every tick (independent of state) drops immediately, but sensor_temp
    # doesn't catch up until HEATING resumes. Found via a real app log: the
    # burner "ignited" (burner_on set to 1) and the SAME-TICK hysteresis
    # check immediately shut it back off, because the stale high sensor_temp
    # was already past the new (lower) upper threshold — from the outside,
    # burner_on just stayed 0 for tens of ticks, looking like heat was never
    # produced at all.
    #
    # sensor_temp now tracks water_temp (not target_temp) once HEATING, so a
    # dropped setpoint alone no longer guarantees a reignition is even due
    # (real water thermal mass legitimately keeps the burner off until the
    # water actually cools) — that's the runaway-bug fix working correctly,
    # not something this regression test should fight. To exercise the
    # actual flicker bug deterministically, force a fresh STARTING window
    # via a demand toggle (guaranteeing a HEATING transition follows) while
    # sensor_temp is still stale-high from the prior high-target cycling.
    print("\n=== setpoint dropped mid-cycle (regression) ===")
    chip3 = ChipHarness(wasm, temp_voltage=8.0)  # high target, builds up a high sensor_temp
    chip3.digital_in["ON/OFF"] = 1
    chip3.run_setup()
    run_for(chip3, 200)  # cold ignition + a couple of cycles
    chip3.digital_in["ON/OFF"] = 0
    run_for(chip3, 5)
    chip3.digital_in["ON/OFF"] = 1
    drop_line = len(chip3.stdout_lines)
    chip3.temp_voltage = 0.0  # dial turned down to minimum -> target drops to 45C
    run_for(chip3, 60)  # past the 42s cold ignition delay

    rows3 = parse_log(chip3.stdout_lines)
    drop_t = rows3[drop_line - 1]["t"]
    after_drop = [r for r in rows3 if r["t"] >= drop_t]
    reignitions = []
    prev_state = 0
    for r in after_drop:
        if r["state"] == 2 and prev_state == 1:
            reignitions.append(r)
        prev_state = r["state"]
    check(failures, "a HEATING transition happens after the setpoint drop", len(reignitions) >= 1,
          f"got {len(reignitions)}")
    check(failures, "burner_on=1 at every post-drop HEATING transition (no instant flicker)",
          all(r["burner_on"] == 1 for r in reignitions),
          f"transitions: {[(r['t'], r['burner_on']) for r in reignitions]}")

    print(f"\n{len(failures)} failing" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
