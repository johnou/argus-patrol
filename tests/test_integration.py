"""Manual real-camera test. It is deliberately disabled unless opted in."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from argus_patrol.camera import ArgusCamera, CameraSettings
from argus_patrol.config import load_settings
from argus_patrol.errors import ConfigurationError


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_argus_pt_ultra_goto_preset() -> None:
    if os.getenv("REOLINK_RUN_INTEGRATION") != "1":
        pytest.skip("set REOLINK_RUN_INTEGRATION=1 to move a real camera")

    try:
        settings = _load_integration_settings()
    except ConfigurationError as error:
        pytest.fail(f"integration camera configuration rejected: {error}")

    camera = ArgusCamera(settings)
    requested_preset = os.getenv("REOLINK_INTEGRATION_PRESET")
    if requested_preset is not None:
        preset_id = int(requested_preset)
    else:
        presets = await camera.get_presets()
        if not presets:
            pytest.fail("camera returned no stored PTZ presets")
        preset_id = presets[0].id
    await camera.goto_preset(preset_id)


def _load_integration_settings() -> CameraSettings:
    requested = os.getenv("ARGUS_CONFIG")
    if requested:
        return load_settings(Path(requested))
    for candidate in (Path("argus.yaml"), Path("config.yaml")):
        if candidate.is_file():
            return load_settings(candidate)
    pytest.skip("provide ARGUS_CONFIG, argus.yaml, or config.yaml")
