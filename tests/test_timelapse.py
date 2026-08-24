from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from argus_patrol.timelapse import TimelapseArchive


def test_snapshot_paths_are_partitioned_by_local_day_and_preset(tmp_path: Path) -> None:
    archive = TimelapseArchive(snapshot_root=tmp_path / "snapshots")
    rome = ZoneInfo("Europe/Rome")

    path = archive.snapshot_path(2, datetime(2026, 8, 24, 21, 43, 44, tzinfo=rome))

    assert path == tmp_path / "snapshots" / "2026-08-24" / "preset-2" / "20260824T214344+0200.jpg"


@pytest.mark.asyncio
async def test_completed_day_renders_one_video_per_preset_with_two_images(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    image_directory = snapshot_root / "2026-08-23" / "preset-1"
    image_directory.mkdir(parents=True)
    (image_directory / "20260823T120000+0200.jpg").write_bytes(b"first")
    (image_directory / "20260823T121000+0200.jpg").write_bytes(b"second")
    commands: list[tuple[str, ...]] = []

    async def run_ffmpeg(command: tuple[str, ...]) -> None:
        commands.append(command)

    archive = TimelapseArchive(
        snapshot_root=snapshot_root,
        output_root=tmp_path / "timelapses",
        ffmpeg_runner=run_ffmpeg,
    )

    rendered = await archive.render_completed_days(datetime(2026, 8, 24, tzinfo=ZoneInfo("Europe/Rome")))

    output = tmp_path / "timelapses" / "2026-08-23" / "preset-1.mp4"
    assert rendered == (output,)
    assert commands == [
        (
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-framerate",
            "12",
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
    ]


@pytest.mark.asyncio
async def test_today_and_single_image_directories_are_not_rendered(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshots"
    yesterday = snapshot_root / "2026-08-23" / "preset-0"
    today = snapshot_root / "2026-08-24" / "preset-1"
    yesterday.mkdir(parents=True)
    today.mkdir(parents=True)
    (yesterday / "20260823T120000+0200.jpg").write_bytes(b"only")
    (today / "20260824T120000+0200.jpg").write_bytes(b"today")

    async def unexpected_ffmpeg(_command: tuple[str, ...]) -> None:
        raise AssertionError("ffmpeg should not be called")

    archive = TimelapseArchive(snapshot_root=snapshot_root, ffmpeg_runner=unexpected_ffmpeg)

    assert await archive.render_completed_days(datetime(2026, 8, 24, tzinfo=ZoneInfo("Europe/Rome"))) == ()
