# Argus Patrol

Minimal Reolink Argus PT preset patrol through UID/P2P. Every command creates
one PyNeolink session, authenticates, performs one operation, and closes it.
It never starts continuous video and never changes PIR settings.

## Install

```sh
cd argus-patrol
uv venv --python 3.11
uv pip install -e '.[dev]'
cp config.example.yaml argus.yaml
```

Set `REOLINK_UID`, `REOLINK_USERNAME`, and `REOLINK_PASSWORD`.
`REOLINK_USERNAME` is normally `admin`; `REOLINK_PASSWORD` is the camera's
device password, not the password for your Reolink app/account. `argus.yaml`
is ignored by Git. The tool also works without a config file when these
environment variables are set.

## Use

Activate the project's virtual environment before using the installed command:

```sh
source .venv/bin/activate
argus --config argus.yaml patrol
```

Use your chosen local configuration filename in place of `argus.yaml`:

```sh
argus --config argus.yaml status
argus --config argus.yaml snapshot
argus --config argus.yaml preset 3
argus --config argus.yaml presets
argus --config argus.yaml patrol
# Opt-in local snapshots plus one video per preset at each local midnight.
argus --config argus.yaml patrol --timelapse
```

`preset` success means the camera accepted the recall request. It deliberately
does not stay connected to poll physical movement completion.

On the Argus PT Ultra, preset recall requires one snapshot in the same
authenticated session before the PTZ command. By default it is discarded; it
never starts a continuous video stream.

`patrol --timelapse` saves that already-required priming JPEG—no extra camera
connection or image request—and files it under the physical preset being left.
The first operation after patrol starts is not archived because the camera's
starting orientation is unknown. At local midnight, FFmpeg renders each
completed day's `snapshots/YYYY-MM-DD/preset-<id>/` images into
`timelapses/YYYY-MM-DD/preset-<id>.mp4`; it needs at least two images per
preset. This option is off by default and requires `ffmpeg` on `PATH`. Raw
JPEGs are retained, so plan for local disk space.

`patrol` moves to each configured preset, disconnects, then waits for a random
whole-second value from `interval_min_seconds` through `interval_max_seconds`
before next connection. On transport or wake failure, it
retries same preset after interval. Authentication or rejected-preset errors
stop patrol. `SIGINT` and `SIGTERM` stop after current connection closes.

`active_windows` controls when patrol may connect, evaluated in `timezone`.
Omit it for 24/7 patrol. Windows may cross midnight:

```yaml
# 24/7
active_windows: ["00:00-24:00"]

# 12:30–14:00 plus 19:00–08:00 daily
active_windows: ["12:30-14:00", "19:00-08:00"]
```

Outside a window, patrol makes no camera connection. It remains disconnected
until next allowed start. Set `return_to_preset_id` to recall a home preset
once as an active window ends; for example, `return_to_preset_id: 0` returns
to preset 0 before waiting for the next window.

For external patrol, turn off Reolink app **Auto-Return to Monitor Point**.
Otherwise camera returns to app monitor point and defeats configured rotation.
Keep PIR enabled; tool never changes PIR settings.

## Verification

```sh
ruff check .
mypy src tests
pytest
```

The real-camera test is opt-in and moves the configured preset:

```sh
REOLINK_RUN_INTEGRATION=1 pytest -m integration
```

Test reads `argus.yaml`, then `config.yaml`; use `ARGUS_CONFIG=/path/file.yaml`
to choose another file. Set `REOLINK_INTEGRATION_PRESET` to test one specific
slot; otherwise test reads the stored list and recalls its first slot.
