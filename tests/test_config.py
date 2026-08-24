from __future__ import annotations

from pathlib import Path

import pytest

from argus_patrol.config import load_patrol_settings, load_settings
from argus_patrol.errors import ConfigurationError


def test_config_expands_environment_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REOLINK_UID", "ABCDEF0123456789")
    monkeypatch.setenv("REOLINK_USERNAME", "admin")
    monkeypatch.setenv("REOLINK_PASSWORD", "secret")
    config = tmp_path / "argus.yaml"
    config.write_text(
        """camera:
  uid: ${REOLINK_UID}
  username: ${REOLINK_USERNAME}
  password: ${REOLINK_PASSWORD}
patrol:
  presets: [1, 2]
  interval_seconds: 60
"""
    )

    settings = load_settings(config)

    assert settings.uid == "ABCDEF0123456789"
    assert settings.username == "admin"
    assert settings.password == "secret"


def test_config_refuses_missing_environment_reference(tmp_path: Path) -> None:
    config = tmp_path / "argus.yaml"
    config.write_text("camera:\n  uid: ${MISSING_REOLINK_UID}\n")

    with pytest.raises(ConfigurationError, match="MISSING_REOLINK_UID"):
        load_settings(config)


def test_status_configuration_does_not_require_patrol_presets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REOLINK_UID", "ABCDEF0123456789")
    monkeypatch.setenv("REOLINK_USERNAME", "admin")
    monkeypatch.setenv("REOLINK_PASSWORD", "secret")

    settings = load_settings()

    assert settings.channel_id == 0


def test_patrol_configuration_reads_preset_ids_and_interval(tmp_path: Path) -> None:
    config = tmp_path / "argus.yaml"
    config.write_text(
        "patrol:\n  presets: [0, 1, 2]\n  interval_min_seconds: 240\n  interval_max_seconds: 360\n"
    )

    settings = load_patrol_settings(config)

    assert settings.presets == (0, 1, 2)
    assert settings.interval_min_seconds == 240
    assert settings.interval_max_seconds == 360


def test_patrol_configuration_reads_cross_midnight_active_windows(tmp_path: Path) -> None:
    config = tmp_path / "argus.yaml"
    config.write_text(
        "patrol:\n"
        "  presets: [0]\n"
        "  interval_min_seconds: 240\n"
        "  interval_max_seconds: 360\n"
        "  timezone: Europe/Rome\n"
        "  active_windows: [\"12:30-14:00\", \"19:00-08:00\"]\n"
    )

    settings = load_patrol_settings(config)

    assert settings.schedule.timezone.key == "Europe/Rome"
    assert len(settings.schedule.windows) == 2


def test_patrol_configuration_supports_legacy_fixed_interval(tmp_path: Path) -> None:
    config = tmp_path / "argus.yaml"
    config.write_text("patrol:\n  presets: [0]\n  interval_seconds: 300\n")

    settings = load_patrol_settings(config)

    assert settings.interval_min_seconds == settings.interval_max_seconds == 300


def test_patrol_configuration_rejects_inverted_interval_bounds(tmp_path: Path) -> None:
    config = tmp_path / "argus.yaml"
    config.write_text("patrol:\n  presets: [0]\n  interval_min_seconds: 360\n  interval_max_seconds: 240\n")

    with pytest.raises(ConfigurationError, match="cannot exceed"):
        load_patrol_settings(config)


def test_patrol_configuration_rejects_empty_preset_list(tmp_path: Path) -> None:
    config = tmp_path / "argus.yaml"
    config.write_text("patrol:\n  presets: []\n")

    with pytest.raises(ConfigurationError, match="non-empty"):
        load_patrol_settings(config)
