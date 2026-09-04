from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from argus_patrol.config import PatrolSettings
from argus_patrol.errors import AuthenticationError, PtzRejectedError, RelayError
from argus_patrol.patrol import PatrolRunner, _wait_for_stop_or_interval
from argus_patrol.schedule import DailySchedule, DailyWindow


class FakePresetMover:
    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.calls: list[int] = []
        self.snapshot_outputs: list[Path | None] = []
        self._failures = iter(failures or [])

    async def goto_preset(self, preset_id: int, *, prime_snapshot_output: Path | None = None) -> None:
        self.calls.append(preset_id)
        self.snapshot_outputs.append(prime_snapshot_output)
        try:
            error = next(self._failures)
        except StopIteration:
            return
        raise error


@pytest.mark.asyncio
async def test_patrol_rotates_after_each_successful_disconnected_operation() -> None:
    mover = FakePresetMover()
    delays: list[float] = []
    now = datetime(2026, 8, 24, tzinfo=ZoneInfo("UTC"))

    async def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += timedelta(seconds=delay)

    intervals = iter((240, 360, 300))
    runner = PatrolRunner(
        mover,
        PatrolSettings((0, 1, 2), 240, 360),
        sleep=sleep,
        interval_randomizer=lambda _minimum, _maximum: next(intervals),
        clock=lambda: now,
    )
    await runner.run(asyncio.Event(), successful_operation_limit=4)

    assert mover.calls == [0, 1, 2, 0]
    assert delays == [240, 360, 300]


@pytest.mark.asyncio
async def test_patrol_retries_same_preset_after_transient_operation_failure() -> None:
    mover = FakePresetMover([RelayError("relay unavailable")])
    delays: list[float] = []
    now = datetime(2026, 8, 24, tzinfo=ZoneInfo("UTC"))

    async def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += timedelta(seconds=delay)

    runner = PatrolRunner(
        mover,
        PatrolSettings((2, 0), 240, 360),
        sleep=sleep,
        interval_randomizer=lambda _minimum, _maximum: 300,
        clock=lambda: now,
    )
    await runner.run(asyncio.Event(), successful_operation_limit=1)

    assert mover.calls == [2, 2]
    assert delays == [300]


@pytest.mark.asyncio
async def test_patrol_does_not_retry_authentication_failure() -> None:
    mover = FakePresetMover([AuthenticationError("bad password")])
    runner = PatrolRunner(mover, PatrolSettings((0,), 240, 360))

    with pytest.raises(AuthenticationError):
        await runner.run(asyncio.Event())

    assert mover.calls == [0]


@pytest.mark.asyncio
async def test_patrol_stops_on_rejected_preset() -> None:
    mover = FakePresetMover([PtzRejectedError("preset already active")])
    runner = PatrolRunner(mover, PatrolSettings((0, 1), 240, 360))

    with pytest.raises(PtzRejectedError):
        await runner.run(asyncio.Event())

    assert mover.calls == [0]


@pytest.mark.asyncio
async def test_patrol_wait_returns_when_stop_is_set() -> None:
    stop = asyncio.Event()
    stop.set()
    calls: list[float] = []

    async def sleep(delay: float) -> None:
        calls.append(delay)
        await asyncio.Event().wait()

    await _wait_for_stop_or_interval(stop, 300, sleep)

    assert calls == [300]


@pytest.mark.asyncio
async def test_patrol_never_connects_outside_active_windows() -> None:
    mover = FakePresetMover()
    stop = asyncio.Event()
    delays: list[float] = []
    utc = ZoneInfo("UTC")
    now = datetime(2026, 8, 24, 14, 0, tzinfo=utc)

    async def sleep(delay: float) -> None:
        delays.append(delay)
        stop.set()

    settings = PatrolSettings(
        (0,),
        240,
        360,
        schedule=DailySchedule(utc, (DailyWindow.parse("12:30-14:00"), DailyWindow.parse("19:00-08:00"))),
    )
    runner = PatrolRunner(mover, settings, sleep=sleep, clock=lambda: now)

    await runner.run(stop)

    assert mover.calls == []
    assert delays == [5 * 60 * 60]


@pytest.mark.asyncio
async def test_patrol_returns_to_configured_preset_after_active_window_ends() -> None:
    stop = asyncio.Event()

    class StopAfterReturnMover(FakePresetMover):
        async def goto_preset(
            self, preset_id: int, *, prime_snapshot_output: Path | None = None
        ) -> None:
            await super().goto_preset(preset_id, prime_snapshot_output=prime_snapshot_output)
            if preset_id == 0:
                stop.set()

    mover = StopAfterReturnMover()
    utc = ZoneInfo("UTC")
    now = datetime(2026, 8, 24, 13, 55, tzinfo=utc)

    async def sleep(delay: float) -> None:
        nonlocal now
        now += timedelta(seconds=delay)

    settings = PatrolSettings(
        (2, 1, 0),
        600,
        600,
        schedule=DailySchedule(utc, (DailyWindow.parse("12:30-14:00"),)),
        return_to_preset_id=0,
    )
    runner = PatrolRunner(mover, settings, sleep=sleep, clock=lambda: now)

    await runner.run(stop)

    assert mover.calls == [2, 0]


def test_schedule_finds_the_end_of_the_current_active_window() -> None:
    utc = ZoneInfo("UTC")
    schedule = DailySchedule(utc, (DailyWindow.parse("12:30-14:00"), DailyWindow.parse("19:00-08:00")))

    assert schedule.next_inactive_at(datetime(2026, 8, 24, 13, 55, 30, tzinfo=utc)) == datetime(
        2026, 8, 24, 14, 0, tzinfo=utc
    )
    assert schedule.next_inactive_at(datetime(2026, 8, 24, 22, 0, tzinfo=utc)) == datetime(
        2026, 8, 25, 8, 0, tzinfo=utc
    )
    assert DailySchedule.always(utc).next_inactive_at(datetime(2026, 8, 24, 13, 55, tzinfo=utc)) is None


@pytest.mark.asyncio
async def test_timelapse_archives_known_preset_using_the_existing_primer_snapshot(tmp_path: Path) -> None:
    mover = FakePresetMover()
    now = datetime(2026, 8, 24, 21, 0, tzinfo=ZoneInfo("Europe/Rome"))

    async def sleep(delay: float) -> None:
        nonlocal now
        now += timedelta(seconds=delay)

    from argus_patrol.timelapse import TimelapseArchive

    archive = TimelapseArchive(snapshot_root=tmp_path / "snapshots")
    runner = PatrolRunner(
        mover,
        PatrolSettings((0, 1, 2), 240, 240, schedule=DailySchedule.always(ZoneInfo("Europe/Rome"))),
        sleep=sleep,
        clock=lambda: now,
        timelapse_archive=archive,
    )

    await runner.run(asyncio.Event(), successful_operation_limit=3)

    assert mover.calls == [0, 1, 2]
    assert mover.snapshot_outputs[0] is None
    assert mover.snapshot_outputs[1] == tmp_path / "snapshots" / "2026-08-24" / "preset-0" / (
        "20260824T210400+0200.jpg"
    )
    assert mover.snapshot_outputs[2] == tmp_path / "snapshots" / "2026-08-24" / "preset-1" / (
        "20260824T210800+0200.jpg"
    )
