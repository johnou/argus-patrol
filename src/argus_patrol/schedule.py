"""Timezone-aware daily patrol windows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigurationError

_WINDOW = re.compile(r"^(?P<start>\d{2}:\d{2})-(?P<end>\d{2}:\d{2})$")


@dataclass(frozen=True)
class DailyWindow:
    """One daily half-open window, represented as minutes after midnight."""

    start_minute: int
    end_minute: int

    @classmethod
    def parse(cls, value: str) -> DailyWindow:
        match = _WINDOW.fullmatch(value)
        if match is None:
            raise ConfigurationError("active windows must use HH:MM-HH:MM, for example 19:00-08:00")
        start = _minute_of_day(match.group("start"), allow_24=False)
        end = _minute_of_day(match.group("end"), allow_24=True)
        if start == end:
            raise ConfigurationError("active window start and end cannot be equal; use 00:00-24:00 for 24/7")
        return cls(start_minute=start, end_minute=end)

    def contains(self, minute_of_day: int) -> bool:
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute_of_day < self.end_minute
        return minute_of_day >= self.start_minute or minute_of_day < self.end_minute


@dataclass(frozen=True)
class DailySchedule:
    """Daily windows evaluated in one explicit IANA timezone."""

    timezone: ZoneInfo
    windows: tuple[DailyWindow, ...]

    @classmethod
    def always(cls, timezone: ZoneInfo) -> DailySchedule:
        return cls(timezone=timezone, windows=(DailyWindow(0, 24 * 60),))

    def is_active(self, when: datetime) -> bool:
        local = when.astimezone(self.timezone)
        minute = local.hour * 60 + local.minute
        return any(window.contains(minute) for window in self.windows)

    def next_active_at(self, not_before: datetime) -> datetime:
        """Return ``not_before`` if active, otherwise next local window start."""
        local = not_before.astimezone(self.timezone)
        if self.is_active(local):
            return local

        candidates: list[datetime] = []
        for day_offset in (0, 1):
            day = local.date() + timedelta(days=day_offset)
            for window in self.windows:
                start_time = time(window.start_minute // 60, window.start_minute % 60)
                start = datetime.combine(day, start_time, self.timezone)
                if start > local:
                    candidates.append(start)
        if not candidates:
            raise RuntimeError("daily schedule has no future active window")
        return min(candidates)

    def next_inactive_at(self, when: datetime) -> datetime | None:
        """Return the next local minute at which no configured window is active.

        ``None`` means that the configured windows cover the entire day.
        """
        local = when.astimezone(self.timezone)
        if not self.is_active(local):
            return local
        minute = local.replace(second=0, microsecond=0)
        for offset in range(1, 24 * 60 + 1):
            candidate = minute + timedelta(minutes=offset)
            if not self.is_active(candidate):
                return candidate
        return None


def parse_daily_schedule(timezone_name: str, raw_windows: object | None) -> DailySchedule:
    """Parse configured windows; omitted windows mean 24/7."""
    try:
        timezone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise ConfigurationError(f"invalid patrol timezone: {timezone_name}") from error

    if raw_windows is None:
        return DailySchedule.always(timezone)
    if not isinstance(raw_windows, list) or not raw_windows:
        raise ConfigurationError("active_windows must be a non-empty list of HH:MM-HH:MM values")
    windows: list[DailyWindow] = []
    for value in raw_windows:
        if not isinstance(value, str):
            raise ConfigurationError("active windows must use HH:MM-HH:MM")
        windows.append(DailyWindow.parse(value))
    return DailySchedule(timezone=timezone, windows=tuple(windows))


def _minute_of_day(value: str, *, allow_24: bool) -> int:
    try:
        hour_text, minute_text = value.split(":")
        hour, minute = int(hour_text), int(minute_text)
    except ValueError as error:
        raise ConfigurationError("active windows must use HH:MM-HH:MM") from error
    if hour == 24 and minute == 0 and allow_24:
        return 24 * 60
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigurationError("active window time is outside 00:00-24:00")
    return hour * 60 + minute
