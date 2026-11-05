# Python API

Everything on this page is importable from `nencarta`, but `ConfigOption`, from `nencarta.core.defaults`.

## Running NenCarta

`process_watershed` maps a watershed from Python, as the `cli` command does, and `process_json_input` maps the
watersheds of a JSON file, as the `json` command does. They take the settings of [Configuration](configuration.md):

```python
from nencarta import process_watershed

# With parallel, each process imports this script again
if __name__ == "__main__":
    process_watershed({
        "name": "example",
        "dem_dir": r"C:\data\dems",
        "flowline": r"C:\data\streams.gpkg",
        "output_dir": r"C:\data\results",
        "floodmap_mode": "return_period",
        "return_periods": [2, 100],
    })
```

::: nencarta.process_watershed

::: nencarta.process_many_watersheds

::: nencarta.process_json_input

## Settings

`CONFIG_OPTIONS` holds every key of [Configuration](configuration.md), with its default, its type and a short
description. `NencartaConfig` checks a watershed's settings as a run does first, without running anything:

```python
from nencarta import CONFIG_OPTIONS, NencartaConfig

print(CONFIG_OPTIONS["buffer_distance"].default)  # 0.1

config = NencartaConfig({
    "name": "example",
    "dem": "dem.tif",
    "flowline": "streams.gpkg",
    "output_dir": "results",
    "buffer": True,
})
print(config.buffer_distance)  # 0.1, its default
```

::: nencarta.NencartaConfig
    options:
      members: false

::: nencarta.CONFIG_OPTIONS

::: nencarta.core.defaults.ConfigOption
    options:
      show_root_full_path: true

## Logging

NenCarta logs to the `nencarta` logger (`nencarta.LOG`), which prints to the console unless logging was set up
before NenCarta was imported.

::: nencarta.set_log_level
