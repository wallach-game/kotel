"""Shared compile + wasmtime host-environment harness for testing chip.c
against the stock velxio-chip.h, without needing the real Velxio app.
Used by test_chip_runtime.py and test_boiler_timing.py.
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


def compile_chip(source_path: Path = CHIP_C) -> bytes:
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

    temp_voltage: fixed reading returned for the "TEMP 0-12V" analog pin.
    digital_in: {pin_name: 0/1} — settable any time, read live by vx_pin_read
        (e.g. `chip.digital_in["ON/OFF"] = 1` to raise the demand signal).
    attr_overrides: {attr_name: value} — overrides an attribute's
        vx_attr_register default, settable before or after setup.
    """

    def __init__(self, wasm_bytes: bytes, temp_voltage: float = 0.0):
        self.now_ns = 0
        self.pins: list[dict] = []
        self.timers: list[dict] = []
        self.attrs: list[dict] = []
        self.log_lines: list[str] = []
        self.stdout_lines: list[str] = []
        self._stdout_buf = ""
        self.fb_w = self.fb_h = 0
        self.fb_pixels = bytearray()
        self.temp_voltage = temp_voltage
        self.digital_in: dict[str, int] = {}
        self.attr_overrides: dict[str, float] = {}

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

        def vx_pin_read(handle):
            pin = self.pins[handle] if 0 <= handle < len(self.pins) else None
            return int(bool(self.digital_in.get(pin["name"], 0))) if pin else 0

        def vx_pin_read_analog(handle):
            pin = self.pins[handle] if 0 <= handle < len(self.pins) else None
            return self.temp_voltage if pin and pin["name"] == "TEMP 0-12V" else 0.0

        def vx_attr_register(name_ptr, default_val):
            handle = len(self.attrs)
            self.attrs.append({"name": self._read_cstring(name_ptr), "default": default_val})
            return handle

        def vx_attr_read(handle):
            a = self.attrs[handle] if 0 <= handle < len(self.attrs) else None
            if a is None:
                return 0.0
            return self.attr_overrides.get(a["name"], a["default"])

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
            "vx_pin_read":         (wasmtime.FuncType([i32], [i32]),      vx_pin_read),
            "vx_pin_write":        (wasmtime.FuncType([i32, i32], []),    lambda h, v: None),
            "vx_pin_read_analog":  (wasmtime.FuncType([i32], [f64]),      vx_pin_read_analog),
            "vx_pin_dac_write":    (wasmtime.FuncType([i32, f64], []),    lambda h, v: None),
            "vx_pin_pwm_write":    (wasmtime.FuncType([i32, f64], []),    lambda h, d: None),
            "vx_pin_set_mode":     (wasmtime.FuncType([i32, i32], []),    lambda h, m: None),
            "vx_pin_watch":        (wasmtime.FuncType([i32, i32, i32, i32], []), lambda h, e, c, u: None),
            "vx_pin_watch_stop":   (wasmtime.FuncType([i32], []),         lambda h: None),
            "vx_attr_register":    (wasmtime.FuncType([i32, f64], [i32]), vx_attr_register),
            "vx_attr_read":        (wasmtime.FuncType([i32], [f64]),      vx_attr_read),
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
        """Advances time by period_ns and fires every active timer once.
        Fine for chips with a single repeating timer (all of ours so far);
        doesn't model per-timer periods if a chip ever registers more than one."""
        self.now_ns += period_ns
        table = self.exports.get("__indirect_function_table")
        for t in self.timers:
            if t["active"]:
                fn = table.get(self.store, t["cb_idx"])
                fn(self.store, t["user_data"])
        self._flush_stdout()
