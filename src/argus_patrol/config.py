"""Configuration from environment variables or a local YAML file."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .camera import CameraSettings
from .errors import ConfigurationError
from .protocol import validate_channel_id, validate_preset_id
from .schedule import DailySchedule, parse_daily_schedule

_ENV_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


@dataclass(frozen=True)
class PatrolSettings:
    """Preset sequence and disconnected random-delay bounds for a patrol."""

    presets: tuple[int, ...]
    interval_min_seconds: int
    interval_max_seconds: int
    schedule: DailySchedule = field(default_factory=lambda: DailySchedule.always(ZoneInfo("UTC")))


def load_settings(path: Path | None = None) -> CameraSettings:
    """Load camera settings without exposing secrets in errors."""
    document = _read_document(path) if path is not None else {}
    camera_data = _mapping(document.get("camera", {}), "camera")

    uid = _required_text(camera_data, "uid", "REOLINK_UID")
    username = _required_text(camera_data, "username", "REOLINK_USERNAME")
    password = _required_text(camera_data, "password", "REOLINK_PASSWORD")

    settings = CameraSettings(
        uid=uid,
        username=username,
        password=password,
        channel_id=validate_channel_id(_integer(camera_data, "channel_id", 0)),
        connect_timeout_seconds=_positive_float(camera_data, "connect_timeout_seconds", 12.0),
        connect_attempts=_positive_integer(camera_data, "connect_attempts", 3),
        retry_backoff_seconds=_positive_float(camera_data, "retry_backoff_seconds", 2.0),
    )
    return settings


def load_patrol_settings(path: Path | None = None) -> PatrolSettings:
    """Load patrol-only configuration without requiring it for one-shot commands."""
    document = _read_document(path) if path is not None else {}
    patrol_data = _mapping(document.get("patrol", {}), "patrol")
    presets = _preset_ids(patrol_data)
    interval_min_seconds, interval_max_seconds = _patrol_interval_bounds(patrol_data)
    timezone = _optional_text(patrol_data, "timezone", "ARGUS_PATROL_TIMEZONE", "UTC")
    return PatrolSettings(
        presets=presets,
        interval_min_seconds=interval_min_seconds,
        interval_max_seconds=interval_max_seconds,
        schedule=parse_daily_schedule(timezone, patrol_data.get("active_windows")),
    )


def _read_document(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigurationError(f"configuration file not found: {path}") from error
    except yaml.YAMLError as error:
        raise ConfigurationError(f"invalid YAML configuration: {path}") from error
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration root must be a mapping")
    expanded = _expand_environment(raw)
    if not isinstance(expanded, dict):
        raise ConfigurationError("configuration root must be a mapping")
    document: dict[str, Any] = {}
    for key, value in expanded.items():
        if not isinstance(key, str):
            raise ConfigurationError("configuration keys must be strings")
        document[key] = value
    return document


def _expand_environment(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_REFERENCE.sub(_replace_environment, value)
    if isinstance(value, list):
        return [_expand_environment(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_environment(item) for key, item in value.items()}
    return value


def _replace_environment(match: re.Match[str]) -> str:
    name = match.group(1)
    value = os.getenv(name)
    if value is None:
        raise ConfigurationError(f"required environment variable is not set: {name}")
    return value


def _mapping(value: Any, section: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{section} must be a mapping")
    return value


def _required_text(data: Mapping[str, Any], key: str, environment: str) -> str:
    value = data.get(key, os.getenv(environment))
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"missing camera {key}; set {environment} or local configuration")
    return value.strip()


def _optional_text(data: Mapping[str, Any], key: str, environment: str, default: str) -> str:
    value = data.get(key, os.getenv(environment, default))
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string")
    return value.strip()


def _integer(data: Mapping[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool):
        raise ConfigurationError(f"{key} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{key} must be an integer") from error


def _positive_integer(data: Mapping[str, Any], key: str, default: int) -> int:
    value = _integer(data, key, default)
    if value < 1:
        raise ConfigurationError(f"{key} must be at least 1")
    return value


def _positive_float(data: Mapping[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool):
        raise ConfigurationError(f"{key} must be a positive number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{key} must be a positive number") from error
    if result <= 0:
        raise ConfigurationError(f"{key} must be a positive number")
    return result


def _preset_ids(data: Mapping[str, Any]) -> tuple[int, ...]:
    raw_presets = data.get("presets")
    if raw_presets is None:
        raw_environment = os.getenv("ARGUS_PATROL_PRESETS")
        if raw_environment is None:
            raise ConfigurationError("missing patrol presets; set patrol.presets or ARGUS_PATROL_PRESETS")
        raw_presets = [item.strip() for item in raw_environment.split(",") if item.strip()]
    if not isinstance(raw_presets, list) or not raw_presets:
        raise ConfigurationError("patrol presets must be a non-empty list")

    preset_ids: list[int] = []
    for preset_id in raw_presets:
        if isinstance(preset_id, bool):
            raise ConfigurationError("patrol preset IDs must be integers from 0 through 255")
        try:
            parsed = int(preset_id)
        except (TypeError, ValueError) as error:
            raise ConfigurationError("patrol preset IDs must be integers from 0 through 255") from error
        preset_ids.append(validate_preset_id(parsed))
    return tuple(preset_ids)


def _patrol_interval_bounds(data: Mapping[str, Any]) -> tuple[int, int]:
    has_fixed = "interval_seconds" in data
    has_minimum = "interval_min_seconds" in data
    has_maximum = "interval_max_seconds" in data
    if has_fixed and (has_minimum or has_maximum):
        raise ConfigurationError(
            "use interval_seconds or interval_min_seconds/interval_max_seconds, not both"
        )
    if has_fixed:
        interval = _positive_seconds(data["interval_seconds"], "interval_seconds")
        return interval, interval
    if has_minimum or has_maximum:
        if not (has_minimum and has_maximum):
            raise ConfigurationError("set both interval_min_seconds and interval_max_seconds")
        minimum = _positive_seconds(data["interval_min_seconds"], "interval_min_seconds")
        maximum = _positive_seconds(data["interval_max_seconds"], "interval_max_seconds")
        if minimum > maximum:
            raise ConfigurationError("interval_min_seconds cannot exceed interval_max_seconds")
        return minimum, maximum

    environment_fixed = os.getenv("ARGUS_PATROL_INTERVAL_SECONDS")
    environment_minimum = os.getenv("ARGUS_PATROL_INTERVAL_MIN_SECONDS")
    environment_maximum = os.getenv("ARGUS_PATROL_INTERVAL_MAX_SECONDS")
    if environment_fixed is not None and (environment_minimum is not None or environment_maximum is not None):
        raise ConfigurationError("set fixed or min/max patrol interval environment variables, not both")
    if environment_fixed is not None:
        interval = _positive_seconds(environment_fixed, "ARGUS_PATROL_INTERVAL_SECONDS")
        return interval, interval
    if environment_minimum is not None or environment_maximum is not None:
        if environment_minimum is None or environment_maximum is None:
            raise ConfigurationError(
                "set both ARGUS_PATROL_INTERVAL_MIN_SECONDS and ARGUS_PATROL_INTERVAL_MAX_SECONDS"
            )
        minimum = _positive_seconds(environment_minimum, "ARGUS_PATROL_INTERVAL_MIN_SECONDS")
        maximum = _positive_seconds(environment_maximum, "ARGUS_PATROL_INTERVAL_MAX_SECONDS")
        if minimum > maximum:
            raise ConfigurationError("ARGUS_PATROL_INTERVAL_MIN_SECONDS cannot exceed maximum")
        return minimum, maximum
    return 900, 900


def _positive_seconds(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a positive whole number of seconds")
    try:
        seconds = int(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{name} must be a positive whole number of seconds") from error
    if str(seconds) != str(value).strip() and not isinstance(value, int):
        raise ConfigurationError(f"{name} must be a positive whole number of seconds")
    if seconds < 1:
        raise ConfigurationError(f"{name} must be a positive whole number of seconds")
    return seconds
