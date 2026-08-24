from __future__ import annotations

import pytest

from argus_patrol.errors import ConfigurationError
from argus_patrol.protocol import (
    PTZ_PRESET_COMMAND_ID,
    PTZ_PRESET_LIST_COMMAND_ID,
    build_goto_preset_payload,
    build_preset_extension,
    validate_preset_id,
)


def test_goto_preset_payload_matches_source_backed_baichuan_shape() -> None:
    assert PTZ_PRESET_COMMAND_ID == 19
    assert PTZ_PRESET_LIST_COMMAND_ID == 190
    assert build_preset_extension(0) == (
        b'<?xml version="1.0" encoding="UTF-8" ?>\n'
        b'<Extension version="1.1">\n'
        b"<channelId>0</channelId>\n"
        b"</Extension>"
    )
    assert build_goto_preset_payload(3) == (
        b'<?xml version="1.0" encoding="UTF-8" ?>\n'
        b"<body>\n"
        b'<PtzPreset version="1.1">\n'
        b"<channelId>0</channelId>\n"
        b"<presetList>\n"
        b"<preset>\n"
        b"<id>3</id>\n"
        b"<command>toPos</command>\n"
        b"\n"
        b"\n"
        b"</preset>\n"
        b"</presetList>\n"
        b"</PtzPreset>\n"
        b"</body>"
    )


@pytest.mark.parametrize("preset_id", [-1, 256, True, "3"])
def test_malformed_preset_is_rejected(preset_id: object) -> None:
    with pytest.raises(ConfigurationError):
        validate_preset_id(preset_id)  # type: ignore[arg-type]
