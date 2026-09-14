# TODO

- Still pending: the voltage->target_temp mapping constants (`35.0`, `80.0`,
  `12.0` in `boiler_tick()`) are still hardcoded, not attributes. The
  ignition-delay/cycling parameters (`ignition_delay_s`, `cycle_period_s`,
  `sensor_tau_s`, `hysteresis_k`) already followed this pattern — same
  move applies here: `vx_attr_register("min_temp", 35.0)`, `"max_temp"`,
  `"max_voltage"`, read live via `vx_attr_read()`, with matching
  `chip.json` "attributes" entries so they show up as sliders instead of
  needing a source edit + recompile to retune.
