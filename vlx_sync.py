#!/usr/bin/env python3
"""Sync a Velxio .vlx project's embedded chip source with plain chip.c /
chip.json files on disk, so the chip can be edited with real tools and
tracked in git instead of hand-splicing JSON strings.

A .vlx keeps chip source in two places that must agree: the live
components[i].properties.{sourceC,chipJson} used by the app, and a
fileGroups archive copy of the same two files. This tool keeps both in
sync in one step.

Usage:
    vlx_sync.py unpack <project.vlx> [--dir .]   # .vlx -> chip.c/chip.json
    vlx_sync.py pack   <project.vlx> [--dir .]   # chip.c/chip.json -> .vlx
    vlx_sync.py check  <project.vlx> [--dir .]   # exit 1 if out of sync
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def find_chip_component(data: dict) -> dict:
    for comp in data.get("components", []):
        if "sourceC" in comp.get("properties", {}):
            return comp
    raise SystemExit("no component with a sourceC property found in the .vlx")


def find_filegroup_files(data: dict, comp_id: str) -> dict | None:
    for group_name, files in data.get("fileGroups", {}).items():
        if comp_id in group_name:
            by_name = {f["name"]: f for f in files}
            if "chip.c" in by_name and "chip.json" in by_name:
                return by_name
    return None


def cmd_unpack(args: argparse.Namespace) -> None:
    data = json.loads(Path(args.vlx).read_text())
    comp = find_chip_component(data)
    c_path = Path(args.dir) / "chip.c"
    j_path = Path(args.dir) / "chip.json"
    c_path.write_text(comp["properties"]["sourceC"])
    j_path.write_text(comp["properties"]["chipJson"])
    print(f"unpacked {c_path} ({c_path.stat().st_size} bytes) and {j_path}")


def cmd_pack(args: argparse.Namespace) -> None:
    vlx_path = Path(args.vlx)
    data = json.loads(vlx_path.read_text())
    comp = find_chip_component(data)
    new_c = (Path(args.dir) / "chip.c").read_text()
    new_j = (Path(args.dir) / "chip.json").read_text()

    comp["properties"]["sourceC"] = new_c
    comp["properties"]["chipJson"] = new_j
    # Invalidate the app's compile cache — it was built from the old
    # source; leaving it in place risks the app running stale wasm
    # instead of recompiling from what we just wrote.
    if "wasmBase64" in comp["properties"]:
        comp["properties"]["wasmBase64"] = ""
    if "sourceHash" in comp["properties"]:
        comp["properties"]["sourceHash"] = ""

    fg = find_filegroup_files(data, comp["id"])
    if fg:
        fg["chip.c"]["content"] = new_c
        fg["chip.json"]["content"] = new_j

    vlx_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"packed chip.c/chip.json into {vlx_path}")


def cmd_check(args: argparse.Namespace) -> None:
    data = json.loads(Path(args.vlx).read_text())
    comp = find_chip_component(data)
    disk_c = (Path(args.dir) / "chip.c").read_text()
    disk_j = (Path(args.dir) / "chip.json").read_text()

    ok = True
    if comp["properties"]["sourceC"] != disk_c:
        print("✗ chip.c out of sync with the .vlx's live component source")
        ok = False
    if comp["properties"]["chipJson"] != disk_j:
        print("✗ chip.json out of sync with the .vlx's live component source")
        ok = False

    fg = find_filegroup_files(data, comp["id"])
    if fg:
        if fg["chip.c"]["content"] != disk_c:
            print("✗ fileGroups chip.c archive copy out of sync")
            ok = False
        if fg["chip.json"]["content"] != disk_j:
            print("✗ fileGroups chip.json archive copy out of sync")
            ok = False

    if ok:
        print("✓ chip.c and chip.json match the .vlx (live + archive copies)")
    sys.exit(0 if ok else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, fn in (("unpack", cmd_unpack), ("pack", cmd_pack), ("check", cmd_check)):
        p = sub.add_parser(name)
        p.add_argument("vlx")
        p.add_argument("--dir", default=".", help="directory holding chip.c/chip.json (default: .)")
        p.set_defaults(func=fn)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
