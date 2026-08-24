"""Disconnected preset-patrol scheduler."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from .config import PatrolSettings
from .errors import ArgusError, AuthenticationError, PtzRejectedError
from .logging import log_event
from .timelapse import TimelapseArchive, TimelapseError


class PresetMover(Protocol):
    """Camera operation needed by the patrol scheduler."""

    async def goto_preset(self, preset_id: int, *, prime_snapshot_output: Path | None = None) -> None: ...


AsyncSleep = Callable[[float], Awaitable[None]]
IntervalRandomizer = Callable[[int, int], int]
Clock = Callable[[], datetime]


class PatrolRunner:
    """Cycle preset recalls while the camera remains disconnected between them."""

    def __init__(
        self,
        camera: PresetMover,
        settings: PatrolSettings,
        *,
        sleep: AsyncSleep = asyncio.sleep,
        interval_randomizer: IntervalRandomizer = random.randint,
        clock: Clock | None = None,
        timelapse_archive: TimelapseArchive | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._camera = camera
        self._settings = settings
        self._sleep = sleep
        self._interval_randomizer = interval_randomizer
        self._clock = clock or (lambda: datetime.now(settings.schedule.timezone))
        self._timelapse_archive = timelapse_archive
        self._logger = logger or logging.getLogger("argus_patrol")

    async def run(
        self,
        stop: asyncio.Event,
        *,
        successful_operation_limit: int | None = None,
    ) -> None:
        """Run until stopped; ``successful_operation_limit`` exists for tests."""
        current_index = 0
        known_preset_id: int | None = None
        successful_operations = 0
        next_operation_at = self._clock()
        while not stop.is_set():
            now = self._clock()
            await self._render_completed_days(now)
            if now < next_operation_at:
                await self._wait_until(
                    stop,
                    now,
                    self._next_wake_at(now, next_operation_at),
                    "Waiting while disconnected",
                )
                continue
            if not self._settings.schedule.is_active(now):
                next_operation_at = self._settings.schedule.next_active_at(now)
                await self._wait_until(
                    stop,
                    now,
                    self._next_wake_at(now, next_operation_at),
                    "Patrol paused outside active window",
                )
                continue

            preset_id = self._settings.presets[current_index]
            snapshot_output = self._snapshot_output(known_preset_id, now)
            try:
                await self._camera.goto_preset(preset_id, prime_snapshot_output=snapshot_output)
            except AuthenticationError:
                known_preset_id = None
                log_event(self._logger, logging.ERROR, "Patrol stopped: authentication failed")
                raise
            except PtzRejectedError:
                known_preset_id = None
                log_event(
                    self._logger,
                    logging.ERROR,
                    "Patrol stopped: preset rejected",
                    preset_id=preset_id,
                )
                raise
            except ArgusError as error:
                known_preset_id = None
                log_event(
                    self._logger,
                    logging.WARNING,
                    "Patrol operation failed; retrying after interval",
                    error=type(error).__name__,
                    preset_id=preset_id,
                )
            else:
                successful_operations += 1
                known_preset_id = preset_id
                current_index = (current_index + 1) % len(self._settings.presets)
                if (
                    successful_operation_limit is not None
                    and successful_operations >= successful_operation_limit
                ):
                    return
            interval_seconds = self._next_interval_seconds()
            completed_at = self._clock()
            not_before = completed_at + timedelta(seconds=interval_seconds)
            next_operation_at = self._settings.schedule.next_active_at(not_before)

    def _snapshot_output(self, known_preset_id: int | None, now: datetime) -> Path | None:
        if self._timelapse_archive is None or known_preset_id is None:
            return None
        return self._timelapse_archive.snapshot_path(known_preset_id, now)

    def _next_wake_at(self, now: datetime, next_operation_at: datetime) -> datetime:
        if self._timelapse_archive is None:
            return next_operation_at
        return min(next_operation_at, self._timelapse_archive.next_rollover(now))

    async def _render_completed_days(self, now: datetime) -> None:
        if self._timelapse_archive is None:
            return
        try:
            await self._timelapse_archive.render_completed_days(now)
        except TimelapseError as error:
            log_event(
                self._logger,
                logging.WARNING,
                "Daily timelapse rendering failed; patrol will continue",
                error=type(error).__name__,
            )

    def _next_interval_seconds(self) -> int:
        return self._interval_randomizer(
            self._settings.interval_min_seconds,
            self._settings.interval_max_seconds,
        )

    async def _wait_until(
        self,
        stop: asyncio.Event,
        now: datetime,
        next_operation_at: datetime,
        message: str,
    ) -> None:
        wait_seconds = max(0.0, (next_operation_at - now).total_seconds())
        log_event(
            self._logger,
            logging.INFO,
            message,
            interval_seconds=round(wait_seconds),
            next_operation_at=next_operation_at.isoformat(),
        )
        await _wait_for_stop_or_interval(stop, wait_seconds, self._sleep)


async def _wait_for_stop_or_interval(
    stop: asyncio.Event,
    interval_seconds: float,
    sleep: AsyncSleep,
) -> None:
    """Wait without holding a camera connection and return promptly on shutdown."""
    sleeper: asyncio.Future[None] = asyncio.ensure_future(sleep(interval_seconds))
    stopper = asyncio.create_task(stop.wait())
    try:
        done, pending = await asyncio.wait((sleeper, stopper), return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        for task in (sleeper, stopper):
            if not task.done():
                task.cancel()
        await asyncio.gather(sleeper, stopper, return_exceptions=True)
