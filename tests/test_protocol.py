from __future__ import annotations

import pytest

from argus_patrol.errors import ConfigurationError
from argus_patrol.protocol import validate_preset_id


@pytest.mark.parametrize("preset_id", [-1, 256, True, "3"])
def test_malformed_preset_is_rejected(preset_id: object) -> None:
    with pytest.raises(ConfigurationError):
        validate_preset_id(preset_id)  # type: ignore[arg-type]
