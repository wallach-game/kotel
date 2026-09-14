"""Parses the boiler chip's per-tick log line
("Kotel: t=.. demand=.. burner_on=.. sensor_temp=.. water_temp=.. state=..")
and measures the two timing behaviours from it — this is the log the spec's
"Measurability" requirement refers to, and the two functions below are its
required helpers.
"""
from __future__ import annotations

import re

_LOG_RE = re.compile(
    r"t=(?P<t>\d+)\s+demand=(?P<demand>\d)\s+burner_on=(?P<burner_on>\d)\s+"
    r"sensor_temp=(?P<sensor_temp>[-\d.]+)\s+water_temp=(?P<water_temp>[-\d.]+)\s+"
    r"state=(?P<state>\d)"
)


def parse_log(lines: list[str]) -> list[dict]:
    """Turns raw printf log lines into a time-ordered list of
    {t, demand, burner_on, sensor_temp, water_temp, state} rows.
    Lines that don't match (e.g. the one-shot "Kotel: initialized") are skipped.
    """
    rows = []
    for line in lines:
        m = _LOG_RE.search(line)
        if not m:
            continue
        rows.append({
            "t": int(m["t"]),
            "demand": int(m["demand"]),
            "burner_on": int(m["burner_on"]),
            "sensor_temp": float(m["sensor_temp"]),
            "water_temp": float(m["water_temp"]),
            "state": int(m["state"]),
        })
    return rows


def _rising_edges(rows: list[dict], key: str) -> list[int]:
    edges = []
    prev = 0
    for r in rows:
        if r[key] == 1 and prev == 0:
            edges.append(r["t"])
        prev = r[key]
    return edges


def measured_startup_delay_s(rows: list[dict]) -> float | None:
    """Time from the demand rising edge to the first tick with burner_on=1.
    Returns None if either event never occurs in the log.
    """
    demand_edges = _rising_edges(rows, "demand")
    if not demand_edges:
        return None
    demand_t = demand_edges[0]
    first_heat = next((r["t"] for r in rows if r["burner_on"] == 1 and r["t"] >= demand_t), None)
    if first_heat is None:
        return None
    return float(first_heat - demand_t)


def measured_cycle_period(rows: list[dict], skip_edges: int = 2) -> tuple[float | None, float]:
    """Mean interval between burner_on rising edges once cycling has settled,
    plus the water_temp peak-to-peak amplitude over that steady-state window.

    The first `skip_edges` rising edges are discarded as transient: edge 0 is
    ignition itself (not a hysteresis cycle), and edge 1 is the first
    off/on swing after ignition, which starts from whatever water_temp was
    at ignition rather than the eventual steady-state operating point.

    Returns (mean_period_s | None, peak_to_peak_water_temp_c). Amplitude is
    0.0 (not None) when there isn't enough data — a missing period is the
    real failure signal there.
    """
    edges = _rising_edges(rows, "burner_on")
    steady_edges = edges[skip_edges:]
    if len(steady_edges) < 2:
        return None, 0.0

    diffs = [steady_edges[i + 1] - steady_edges[i] for i in range(len(steady_edges) - 1)]
    mean_period = sum(diffs) / len(diffs)

    steady_rows = [r for r in rows if r["t"] >= steady_edges[0]]
    water_temps = [r["water_temp"] for r in steady_rows]
    amplitude = (max(water_temps) - min(water_temps)) if water_temps else 0.0

    return mean_period, amplitude
