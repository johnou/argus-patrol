"""Command-line interface for one-shot Argus operations and patrol."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from .camera import ArgusCamera
from .config import load_patrol_settings, load_settings
from .errors import ArgusError
from .logging import configure_logging, log_event
from .patrol import PatrolRunner
from .timelapse import TimelapseArchive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="argus", description="One-shot Reolink Argus PT UID/P2P control")
    parser.add_argument("--config", type=Path, help="local YAML file; omitted means environment variables")
    parser.add_argument("--debug", action="store_true", help="show connection retry diagnostics")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("status", help="connect, print login-derived status, disconnect")
    snapshot = subcommands.add_parser("snapshot", help="capture one JPEG, then disconnect")
    snapshot.add_argument("--out", type=Path, help="JPEG path; defaults to snapshots/snapshot-<UTC>.jpg")
    preset = subcommands.add_parser("preset", help="recall one stored PTZ preset, then disconnect")
    preset.add_argument("preset_id", type=int)
    subcommands.add_parser("presets", help="list stored PTZ preset IDs without moving the camera")
    patrol = subcommands.add_parser("patrol", help="cycle configured presets, disconnected between moves")
    patrol.add_argument(
        "--timelapse",
        action="store_true",
        help="save existing PTZ-primer snapshots and render daily preset videos locally",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> int:
    logger = configure_logging(logging.DEBUG if args.debug else logging.INFO)
    try:
        settings = load_settings(args.config)
        camera = ArgusCamera(settings, logger=logger)
        if args.command == "patrol":
            patrol_settings = load_patrol_settings(args.config)
            timelapse_archive = TimelapseArchive(logger=logger) if args.timelapse else None
            stop = asyncio.Event()
            remove_signal_handlers = _install_stop_signal_handlers(stop)
            try:
                await PatrolRunner(
                    camera,
                    patrol_settings,
                    timelapse_archive=timelapse_archive,
                    logger=logger,
                ).run(stop)
            finally:
                remove_signal_handlers()
            log_event(logger, logging.INFO, "Patrol stopped")
            return 0
        if args.command == "status":
            print(json.dumps(await camera.status(), indent=2, sort_keys=True, default=str))
            return 0
        if args.command == "snapshot":
            output = args.out or _default_snapshot_path()
            saved = await camera.snapshot(output)
            print(saved)
            return 0
        if args.command == "preset":
            await camera.goto_preset(args.preset_id)
            return 0
        if args.command == "presets":
            presets = await camera.get_presets()
            print(
                json.dumps(
                    [{"id": item.id, "name": item.name, "enabled": item.enabled} for item in presets],
                    indent=2,
                )
            )
            return 0
        raise AssertionError(f"unexpected command: {args.command}")
    except ArgusError as error:
        log_event(logger, logging.ERROR, "Operation failed", error=type(error).__name__)
        return 2
    except KeyboardInterrupt:
        log_event(logger, logging.INFO, "Stopped")
        return 0


def _default_snapshot_path() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path("snapshots") / f"snapshot-{stamp}.jpg"


def _install_stop_signal_handlers(stop: asyncio.Event) -> Callable[[], None]:
    """Make SIGINT/SIGTERM end only after the current one-shot operation closes."""
    loop = asyncio.get_running_loop()
    registered: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            continue
        registered.append(signum)

    def remove() -> None:
        for signum in registered:
            loop.remove_signal_handler(signum)

    return remove


if __name__ == "__main__":
    sys.exit(main())
