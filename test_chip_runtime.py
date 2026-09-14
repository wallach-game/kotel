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

import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import wasmtime

HERE = Path(__file__).parent
CHIP_C = HERE / "chip.c"
HEADER_DIR = HERE  # velxio-chip.h lives next to chip.c


def compile_chip(source_path: Path) -> bytes:
    wasi_sdk = Path(os.environ.get("WASI_SDK", "/opt/wasi-sdk"))
    clang = wasi_sdk / "bin" / "clang"
    if not clang.is_file():
        sys.exit(f"wasi-sdk clang not found at {clang} — set WASI_SDK or run inside the velxio container")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "chip.wasm"
        cmd = [
            str(clang), "--target=wasm32-unknown-wasip1", "-O2", "-nostartfiles",
            "-Wl,--import-memory", "-Wl,--export-table", "-Wl,--no-entry",
            "-Wl,--export=chip_setup", "-Wl,--allow-undefined",
            "-Wall", "-Wextra",
            "-I", str(HEADER_DIR), str(source_path), "-o", str(out),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            sys.exit(f"clang failed:\n{result.stderr}")
        if result.stderr.strip():
            print(f"[compiler warnings]\n{result.stderr}", file=sys.stderr)
        return out.read_bytes()


class ChipHarness:
    """Minimal host environment for a standalone (non-ESP32-slave) chip:
    WASI stdio + the vx_* imports chip.c actually calls. Mirrors the shapes
    in app/services/wasm_chip_runtime.py; timer firing is driven manually
    here since that module expects an external QEMU-board scheduler this
    standalone chip doesn't have.
    """

    def __init__(self, wasm_bytes: bytes, temp_voltage: float = 0.0):
        self.now_ns = 0
        self.pins: list[dict] = []
        self.timers: list[dict] = []
        self.log_lines: list[str] = []
        self.stdout_lines: list[str] = []
        self._stdout_buf = ""
        self.fb_w = self.fb_h = 0
        self.fb_pixels = bytearray()
        self.temp_voltage = temp_voltage

        engine = wasmtime.Engine()
        self.store = wasmtime.Store(engine)
        module = wasmtime.Module(engine, wasm_bytes)
        self.memory = wasmtime.Memory(self.store, wasmtime.MemoryType(wasmtime.Limits(2, 16)))

        linker = wasmtime.Linker(engine)
        self._define_wasi(linker)
        self._define_velxio(linker)
        linker.define(self.store, "env", "memory", self.memory)

        self.instance = linker.instantiate(self.store, module)
        self.exports = self.instance.exports(self.store)

    def _read_cstring(self, ptr: int) -> str:
        if ptr == 0:
            return ""
        u8 = self.memory.read(self.store, ptr, ptr + 256)
        end = u8.index(0) if 0 in u8 else len(u8)
        return bytes(u8[:end]).decode("utf-8", errors="replace")

    def _flush_stdout(self) -> None:
        while "\n" in self._stdout_buf:
            line, self._stdout_buf = self._stdout_buf.split("\n", 1)
            self.stdout_lines.append(line)

    def _define_wasi(self, linker: wasmtime.Linker) -> None:
        i32, i64 = wasmtime.ValType.i32(), wasmtime.ValType.i64()

        def fd_write(fd, iovs_ptr, iovs_len, nwritten_ptr):
            total, chunks = 0, []
            for i in range(iovs_len):
                hdr = bytes(self.memory.read(self.store, iovs_ptr + i * 8, iovs_ptr + i * 8 + 8))
                buf, length = struct.unpack("<II", hdr)
                if length:
                    chunks.append(bytes(self.memory.read(self.store, buf, buf + length)))
                total += length
            self.memory.write(self.store, struct.pack("<I", total), nwritten_ptr)
            if fd in (1, 2) and chunks:
                self._stdout_buf += b"".join(chunks).decode("utf-8", errors="replace")
                self._flush_stdout()
            return 0

        def clock_time_get(_id, _precision, time_ptr):
            self.memory.write(self.store, struct.pack("<Q", self.now_ns), time_ptr)
            return 0

        def zeros2(a, _b):
            self.memory.write(self.store, struct.pack("<II", 0, 0), a)
            return 0

        sig_iiii = wasmtime.FuncType([i32, i32, i32, i32], [i32])
        sig_ii = wasmtime.FuncType([i32, i32], [i32])
        for ns in ("wasi_snapshot_preview1", "wasi_unstable"):
            linker.define_func(ns, "fd_write", sig_iiii, fd_write)
            linker.define_func(ns, "proc_exit", wasmtime.FuncType([i32], []),
                                lambda c: (_ for _ in ()).throw(wasmtime.Trap(f"proc_exit({c})")))
            linker.define_func(ns, "clock_time_get", wasmtime.FuncType([i32, i64, i32], [i32]), clock_time_get)
            linker.define_func(ns, "environ_sizes_get", sig_ii, zeros2)
            linker.define_func(ns, "environ_get", sig_ii, lambda a, b: 0)
            linker.define_func(ns, "args_sizes_get", sig_ii, zeros2)
            linker.define_func(ns, "args_get", sig_ii, lambda a, b: 0)
            linker.define_func(ns, "random_get", sig_ii, lambda a, b: 0)
            linker.define_func(ns, "fd_close", wasmtime.FuncType([i32], [i32]), lambda a: 0)
            linker.define_func(ns, "fd_seek", wasmtime.FuncType([i32, i64, i32, i32], [i32]), lambda a, b, c, d: 28)
            linker.define_func(ns, "fd_read", sig_iiii, lambda a, b, c, d: 0)
            linker.define_func(ns, "fd_fdstat_get", sig_ii, lambda a, b: 0)
            linker.define_func(ns, "fd_prestat_get", sig_ii, lambda a, b: 8)
            linker.define_func(ns, "fd_prestat_dir_name", sig_iiii, lambda a, b, c, d: 28)

    def _define_velxio(self, linker: wasmtime.Linker) -> None:
        i32, f64 = wasmtime.ValType.i32(), wasmtime.ValType.f64()

        def vx_pin_register(name_ptr, mode):
            handle = len(self.pins)
            self.pins.append({"name": self._read_cstring(name_ptr), "mode": mode})
            return handle

        def vx_pin_read_analog(handle):
            pin = self.pins[handle] if 0 <= handle < len(self.pins) else None
            return self.temp_voltage if pin and pin["name"] == "TEMP 0-12V" else 0.0

        def vx_timer_create(cb_idx, user_data):
            handle = len(self.timers)
            self.timers.append({"cb_idx": cb_idx, "user_data": user_data, "active": False})
            return handle

        def vx_timer_start(handle, period_ns, repeat):
            t = self.timers[handle]
            t["period_ns"], t["repeat"], t["active"] = period_ns, bool(repeat), True

        def vx_framebuffer_init(w_ptr, h_ptr):
            self.fb_w, self.fb_h = 64, 64
            self.fb_pixels = bytearray(self.fb_w * self.fb_h * 4)
            self.memory.write(self.store, struct.pack("<I", self.fb_w), w_ptr)
            self.memory.write(self.store, struct.pack("<I", self.fb_h), h_ptr)
            return 0

        def vx_buffer_write(_buf, offset, data_ptr, data_len):
            data = self.memory.read(self.store, data_ptr, data_ptr + data_len)
            self.fb_pixels[offset:offset + data_len] = data

        def vx_log(msg_ptr):
            self.log_lines.append(self._read_cstring(msg_ptr))

        sigs = {
            "vx_pin_register":     (wasmtime.FuncType([i32, i32], [i32]), vx_pin_register),
            "vx_pin_read":         (wasmtime.FuncType([i32], [i32]),      lambda h: 0),
            "vx_pin_write":        (wasmtime.FuncType([i32, i32], []),    lambda h, v: None),
            "vx_pin_read_analog":  (wasmtime.FuncType([i32], [f64]),      vx_pin_read_analog),
            "vx_pin_dac_write":    (wasmtime.FuncType([i32, f64], []),    lambda h, v: None),
            "vx_pin_pwm_write":    (wasmtime.FuncType([i32, f64], []),    lambda h, d: None),
            "vx_pin_set_mode":     (wasmtime.FuncType([i32, i32], []),    lambda h, m: None),
            "vx_pin_watch":        (wasmtime.FuncType([i32, i32, i32, i32], []), lambda h, e, c, u: None),
            "vx_pin_watch_stop":   (wasmtime.FuncType([i32], []),         lambda h: None),
            "vx_attr_register":    (wasmtime.FuncType([i32, f64], [i32]), lambda n, d: 0),
            "vx_attr_read":        (wasmtime.FuncType([i32], [f64]),      lambda h: 0.0),
            "vx_attr_register_string": (wasmtime.FuncType([i32, i32], [i32]), lambda n, d: 0),
            "vx_attr_string_len":  (wasmtime.FuncType([i32], [i32]),      lambda h: 0),
            "vx_attr_string_read": (wasmtime.FuncType([i32, i32, i32], [i32]), lambda h, b, c: 0),
            "vx_i2c_attach":       (wasmtime.FuncType([i32], [i32]),      lambda cfg: 0),
            "vx_uart_attach":      (wasmtime.FuncType([i32], [i32]),      lambda cfg: 0),
            "vx_uart_write":       (wasmtime.FuncType([i32, i32, i32], [i32]), lambda h, b, c: 1),
            "vx_spi_attach":       (wasmtime.FuncType([i32], [i32]),      lambda cfg: 0),
            "vx_spi_start":        (wasmtime.FuncType([i32, i32, i32], []), lambda h, b, c: None),
            "vx_spi_stop":         (wasmtime.FuncType([i32], []),         lambda h: None),
            "vx_sim_now_nanos":    (wasmtime.FuncType([], [wasmtime.ValType.i64()]), lambda: self.now_ns),
            "vx_timer_create":     (wasmtime.FuncType([i32, i32], [i32]), vx_timer_create),
            "vx_timer_start":      (wasmtime.FuncType([i32, wasmtime.ValType.i64(), i32], []), vx_timer_start),
            "vx_timer_stop":       (wasmtime.FuncType([i32], []),         lambda h: None),
            "vx_framebuffer_init": (wasmtime.FuncType([i32, i32], [i32]), vx_framebuffer_init),
            "vx_buffer_write":     (wasmtime.FuncType([i32, i32, i32, i32], []), vx_buffer_write),
            "vx_buffer_read":      (wasmtime.FuncType([i32, i32, i32, i32], []), lambda b, o, d, l: None),
            "vx_rom_size":         (wasmtime.FuncType([], [i32]),         lambda: 0),
            "vx_rom_read":         (wasmtime.FuncType([i32, i32, i32], []), lambda o, d, l: None),
            "vx_log":              (wasmtime.FuncType([i32], []),         vx_log),
        }
        for name, (sig, fn) in sigs.items():
            linker.define_func("env", name, sig, fn)

    def run_setup(self) -> None:
        self.exports["chip_setup"](self.store)
        self._flush_stdout()

    def tick(self, period_ns: int = 1_000_000_000) -> None:
        self.now_ns += period_ns
        table = self.exports.get("__indirect_function_table")
        for t in self.timers:
            if t["active"]:
                fn = table.get(self.store, t["cb_idx"])
                fn(self.store, t["user_data"])
        self._flush_stdout()


def main() -> None:
    wasm = compile_chip(CHIP_C)
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
    chip.run_setup()

    check("registers exactly 3 pins", len(chip.pins) == 3, f"got {len(chip.pins)}")
    check("pin 0 is ON/OFF", chip.pins[0]["name"] == "ON/OFF" if chip.pins else False)
    check("pin 2 is TEMP 0-12V", chip.pins[2]["name"] == "TEMP 0-12V" if len(chip.pins) > 2 else False)
    check("creates exactly 1 timer", len(chip.timers) == 1, f"got {len(chip.timers)}")
    check("timer is active after setup", chip.timers[0]["active"] if chip.timers else False,
          "vx_timer_start never ran — check the handle-0 falsy-check trap")
    check('logs "Kotel: initialized"', "Kotel: initialized" in chip.log_lines)
    check("declares a 64x64 display", (chip.fb_w, chip.fb_h) == (64, 64), f"got {chip.fb_w}x{chip.fb_h}")

    for _ in range(5):
        chip.tick()

    check("printf output survives past tick 1 (fflush)", len(chip.stdout_lines) == 5,
          f"got {len(chip.stdout_lines)} lines — stdio buffering regression")
    if chip.stdout_lines:
        last = chip.stdout_lines[-1]
        check("target clamps to 80.0C at/above the 12V ceiling", "target=80.0C" in last, last)
        check("water_temp climbs toward target", "water=20.5C" in last, last)
        check("heating=1 while below target", "heating=1" in last, last)

    # 6V, well inside the 0-12V range -> target = 35 + 6*3.75 = 57.5C,
    # confirming the response is actually proportional to voltage, not just clamped.
    chip2 = ChipHarness(wasm, temp_voltage=6.0)
    chip2.run_setup()
    chip2.tick()
    if chip2.stdout_lines:
        check("target=57.5C from 6V input (proportional, not clamped)",
              "target=57.5C" in chip2.stdout_lines[-1], chip2.stdout_lines[-1])

    check("display buffer is non-blank after ticking", any(chip.fb_pixels), "framebuffer never written")

    # Stats text ("E:.. S:.. T:..") is drawn white-on-black in the top-left corner.
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
