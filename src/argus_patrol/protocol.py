"""Baichuan PTZ preset-recall request.

Constants and XML shape are documented in ``docs/protocol.md``. Keep changes
to this module source-backed; do not substitute the HTTP ``PtzCtrl`` request.
"""

from __future__ import annotations

from .errors import ConfigurationError

# nodelink-js src/protocol/constants.ts: MSG_ID_PTZ_CONTROL_PRESET.
PTZ_PRESET_COMMAND_ID = 19
# nodelink-js src/protocol/constants.ts: MSG_ID_GET_PTZ_PRESET.
PTZ_PRESET_LIST_COMMAND_ID = 190


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


def build_preset_extension(channel_id: int) -> bytes:
    """Build the modern-frame extension used by nodelink-js preset recall."""
    channel = validate_channel_id(channel_id)
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        '<Extension version="1.1">\n'
        f"<channelId>{channel}</channelId>\n"
        "</Extension>"
    ).encode()


def build_goto_preset_payload(preset_id: int, channel_id: int = 0) -> bytes:
    """Build command-19 ``PtzPreset/toPos`` XML for one stored preset."""
    preset = validate_preset_id(preset_id)
    channel = validate_channel_id(channel_id)
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        "<body>\n"
        '<PtzPreset version="1.1">\n'
        f"<channelId>{channel}</channelId>\n"
        "<presetList>\n"
        "<preset>\n"
        f"<id>{preset}</id>\n"
        "<command>toPos</command>\n"
        "\n"
        "\n"
        "</preset>\n"
        "</presetList>\n"
        "</PtzPreset>\n"
        "</body>"
    ).encode()
