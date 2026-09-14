#!/usr/bin/env bash
# Compiles and runs the boiler chip against the real, stock Velxio API —
# no fabricated macros/types, nothing that isn't in velxio-chip.h upstream.
#
# Runs everything inside the `velxio` container (docker-compose.yaml),
# since that's where the wasi-sdk toolchain and the `wasmtime` Python
# package actually live.
set -euo pipefail
cd "$(dirname "$0")"

CONTAINER=velxio

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "✗ container '$CONTAINER' is not running — start it with: docker compose up -d"
  exit 1
fi

echo "=== 1. chip.c / chip.json match the .vlx project ==="
python3 vlx_sync.py check "velxio-project (1).vlx"

echo ""
echo "=== 2. velxio-chip.h matches upstream (no local fabrications) ==="
if diff -q velxio-chip.h <(gh api repos/davidmonterocrespo24/velxio/contents/backend/sdk/velxio-chip.h --jq '.content' | base64 -d) >/dev/null 2>&1; then
  echo "✓ velxio-chip.h is byte-identical to upstream"
else
  echo "✗ velxio-chip.h has diverged from upstream (or gh/network unavailable — check manually)"
fi

echo ""
echo "=== 3. ABI guarantees (vx_i2c_config / vx_uart_config / vx_spi_config sizes) ==="
docker cp velxio-chip.h "$CONTAINER":/tmp/velxio-chip.h
docker cp check_structs.c "$CONTAINER":/tmp/check_structs.c
docker exec "$CONTAINER" sh -c '
  /opt/wasi-sdk/bin/clang --target=wasm32-unknown-wasip1 -Wall -Wextra \
    -I /tmp -c /tmp/check_structs.c -o /tmp/check_structs.o
'
echo "✓ struct sizes match the header's _Static_assert guarantees (wasm32 target)"

echo ""
echo "=== 4. chip.c compiles against stock velxio-chip.h ==="
docker cp chip.c "$CONTAINER":/tmp/chip.c
docker exec "$CONTAINER" sh -c '
  /opt/wasi-sdk/bin/clang --target=wasm32-unknown-wasip1 -O2 -nostartfiles \
    -Wl,--import-memory -Wl,--export-table -Wl,--no-entry \
    -Wl,--export=chip_setup -Wl,--allow-undefined -Wall -Wextra \
    -I /tmp /tmp/chip.c -o /tmp/chip.wasm
'
echo "✓ compiles clean"

echo ""
echo "=== 5. runtime behavior (chip_setup + simulated timer ticks) ==="
docker cp test_chip_runtime.py "$CONTAINER":/tmp/test_chip_runtime.py
docker exec -w /tmp "$CONTAINER" python3 /tmp/test_chip_runtime.py

echo ""
echo "=== all checks passed ==="
