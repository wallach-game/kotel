/* Verifies the ABI guarantees documented in velxio-chip.h: vx_i2c_config,
 * vx_uart_config and vx_spi_config must stay at their fixed byte sizes.
 * The header already enforces this via _Static_assert — if this file
 * compiles, the guarantees hold. Must be built for wasm32 (pointer/enum
 * sizes differ on a 64-bit host and give false failures).
 */
#include "velxio-chip.h"

void chip_setup(void) {}
