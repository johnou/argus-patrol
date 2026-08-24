"""Optional local snapshot archive and daily FFmpeg timelapse rendering."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .logging import log_event

FfmpegRunner = Callable[[tuple[str, ...]], Awaitable[None]]


class TimelapseError(RuntimeError):
    """A local archive or FFmpeg rendering operation failed."""


class TimelapseArchive:
    """Store priming snapshots by local day/preset and render completed days.

    The archive never connects to a camera. ``snapshot_path`` only allocates a
    deterministic local path; the caller gives that path to the already-needed
    Baichuan snapshot used to prime a PTZ recall.
    """

    def __init__(
        self,
        *,
        snapshot_root: Path = Path("snapshots"),
        output_root: Path = Path("timelapses"),
        fps: int = 12,
        ffmpeg_runner: FfmpegRunner | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if fps < 1:
            raise ValueError("timelapse fps must be at least 1")
        self._snapshot_root = snapshot_root
        self._output_root = output_root
        self._fps = fps
        self._ffmpeg_runner = ffmpeg_runner or _run_ffmpeg
        self._logger = logger or logging.getLogger("argus_patrol")

    def snapshot_path(self, preset_id: int, captured_at: datetime) -> Path:
        """Return the local path for a snapshot from an already-known preset."""
        day = captured_at.date().isoformat()
        stamp = captured_at.strftime("%Y%m%dT%H%M%S%z")
        return self._snapshot_root / day / f"preset-{preset_id}" / f"{stamp}.jpg"

    def next_rollover(self, now: datetime) -> datetime:
        """Return the next local midnight, preserving the patrol timezone."""
        tomorrow = now.date() + timedelta(days=1)
        return datetime.combine(tomorrow, time.min, tzinfo=now.tzinfo)

    async def render_completed_days(self, now: datetime) -> tuple[Path, ...]:
        """Render unrendered snapshot days before ``now`` into preset videos."""
        if not self._snapshot_root.is_dir():
            return ()

        rendered: list[Path] = []
        for day_directory in sorted(self._snapshot_root.iterdir()):
            if not day_directory.is_dir():
                continue
            day = _parse_day(day_directory.name)
            if day is None or day >= now.date():
                continue
            for preset_directory in sorted(day_directory.glob("preset-*")):
                if not preset_directory.is_dir():
                    continue
                output = self._output_root / day.isoformat() / f"{preset_directory.name}.mp4"
                if output.exists():
                    continue
                images = tuple(sorted(preset_directory.glob("*.jpg")))
                if len(images) < 2:
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "Skipping timelapse with fewer than two snapshots",
                        day=day.isoformat(),
                        preset=preset_directory.name,
                    )
                    continue
                output.parent.mkdir(parents=True, exist_ok=True)
                await self._ffmpeg_runner(self._ffmpeg_command(preset_directory, output))
                rendered.append(output)
                log_event(
                    self._logger,
                    logging.INFO,
                    "Daily timelapse rendered",
                    day=day.isoformat(),
                    preset=preset_directory.name,
                    output=output,
                    frame_count=len(images),
                )
        return tuple(rendered)

    def _ffmpeg_command(self, image_directory: Path, output: Path) -> tuple[str, ...]:
        """Build a no-overwrite FFmpeg command for consistently sized JPEGs."""
        return (
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-framerate",
            str(self._fps),
            "-pattern_type",
            "glob",
            "-i",
            str(image_directory / "*.jpg"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        )


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


async def _run_ffmpeg(command: tuple[str, ...]) -> None:
    """Run FFmpeg without a shell and surface only a bounded diagnostic."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise TimelapseError("ffmpeg is required for --timelapse but was not found on PATH") from error

    _, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
        summary = detail[-1] if detail else "no diagnostic from ffmpeg"
        raise TimelapseError(f"ffmpeg failed: {summary[:500]}")
