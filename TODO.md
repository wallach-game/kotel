# TODO

- When the boiler controller becomes its own real program (not just this
  demo chip), stop hardcoding the voltage->target_temp mapping constants
  (currently `35.0`, `80.0`, `5.0` in `boiler_tick()`) and expose them as
  chip attributes instead: `vx_attr_register("min_temp", 35.0)`,
  `vx_attr_register("max_temp", 80.0)`, `vx_attr_register("max_voltage", 5.0)`,
  read live each tick via `vx_attr_read()`. `chip.json`'s `"attributes"`
  array gets matching entries (with `min`/`max`/`step`) so they show up as
  sliders in the diagram editor's part inspector instead of requiring a
  source edit + recompile to retune.
