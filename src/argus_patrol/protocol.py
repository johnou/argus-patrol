from __future__ import annotations

from .errors import ConfigurationError


def validate_preset_id(preset_id: int) -> int:
    """Validate the on-wire unsigned-byte preset slot ID."""
    if isinstance(preset_id, bool) or not isinstance(preset_id, int):
        raise ConfigurationError("preset ID must be an integer from 0 through 255")
    if not 0 <= preset_id <= 255:
        raise ConfigurationError("preset ID must be from 0 through 255")
    return preset_id


def validate_channel_id(channel_id: int) -> int:
    """Validate the one-byte Baichuan header/payload channel ID."""
    if isinstance(channel_id, bool) or not isinstance(channel_id, int):
        raise ConfigurationError("channel ID must be an integer from 0 through 255")
    if not 0 <= channel_id <= 255:
        raise ConfigurationError("channel ID must be from 0 through 255")
    return channel_id
