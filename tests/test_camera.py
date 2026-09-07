from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from pyneolink import Ptz  # type: ignore[import-untyped]
from pyneolink.core.bc import ProtocolError  # type: ignore[import-untyped]

from argus_patrol.camera import ArgusCamera, CameraSettings
from argus_patrol.errors import (
    AuthenticationError,
    BaichuanProtocolError,
    ConfigurationError,
    PtzRejectedError,
    PtzTimeoutError,
)


@dataclass
class ReplyHeader:
    response_code: int = 200


@dataclass
class Reply:
    header: ReplyHeader
    payload: bytes = b""

    @property
    def xml_root(self) -> ET.Element:
        return ET.fromstring(self.payload)


class FakeCamera:
    def __init__(
        self,
        *,
        connect_error: Exception | None = None,
        login_error: Exception | None = None,
    ) -> None:
        self.config = settings()
        self.connect_error = connect_error
        self.login_error = login_error
        self.closed = False
        self.calls: list[tuple[Any, ...]] = []
        self.connected_address: tuple[str, int] | None = ("198.51.100.7", 9999)
        self.reply = Reply(ReplyHeader())
        self.command_error: Exception | None = None

    def connect(self) -> None:
        self.calls.append(("connect",))
        if self.connect_error:
            raise self.connect_error

    def login(self) -> str:
        self.calls.append(("login",))
        if self.login_error:
            raise self.login_error
        return "<body />"

    def close(self) -> None:
        self.closed = True
        self.calls.append(("close",))

    def info(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        self.calls.append(("info", include_sensitive))
        return {"name": "Argus"}

    def snapshot(
        self,
        *,
        out: str | Path | None = None,
        stream_type: str = "main",
        retry_on_timeout: bool = True,
        reconnect_retries: int = 1,
    ) -> bytes | Path:
        self.calls.append(("snapshot", out, stream_type, retry_on_timeout, reconnect_retries))
        return Path(out) if out is not None else b"jpeg"

    def ptz(self, *, channel_id: int | None = None) -> Ptz:
        return Ptz(self, channel_id=channel_id)

    def command(
        self,
        msg_id: int,
        payload: bytes = b"",
        *,
        extension: bytes = b"",
        retry_on_timeout: bool = True,
        reconnect_retries: int = 1,
    ) -> Reply:
        self.calls.append(("command", msg_id, payload, extension, retry_on_timeout, reconnect_retries))
        if self.command_error:
            raise self.command_error
        return self.reply


def settings(*, attempts: int = 3) -> CameraSettings:
    return CameraSettings(
        uid="ABCDEF0123456789",
        username="admin",
        password="secret",
        connect_attempts=attempts,
        retry_backoff_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_successful_preset_command_closes_connection() -> None:
    fake = FakeCamera()
    camera = ArgusCamera(settings(), camera_factory=lambda _settings: fake)

    await camera.goto_preset(3)

    assert fake.closed
    command = next(call for call in fake.calls if call[0] == "command")
    assert command[1] == 19
    assert b"<id>3</id>" in command[2]
    assert b"<command>toPos</command>" in command[2]
    assert b"<channelId>0</channelId>" in command[3]
    assert command[4:] == (False, 0)
    assert [call[0] for call in fake.calls].index("snapshot") < [call[0] for call in fake.calls].index(
        "command"
    )


@pytest.mark.asyncio
async def test_preset_list_reads_numeric_ids_without_motion() -> None:
    fake = FakeCamera()
    fake.reply = Reply(
        ReplyHeader(),
        b"""<?xml version="1.0" encoding="UTF-8" ?>
<body><PtzPreset version="1.1"><presetList>
<preset><id>0</id><name>Box</name><enable>1</enable></preset>
<preset><id>2</id><name>Tractor</name><enable>0</enable></preset>
</presetList></PtzPreset></body>""",
    )
    camera = ArgusCamera(settings(), camera_factory=lambda _settings: fake)

    presets = await camera.get_presets()

    assert [(item.id, item.name, item.enabled) for item in presets] == [
        (0, "Box", True),
        (2, "Tractor", False),
    ]
    command = next(call for call in fake.calls if call[0] == "command")
    assert command[1] == 190
    assert command[2] == b""
    assert command[4:] == (False, 0)


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retried_and_closes() -> None:
    fake = FakeCamera(login_error=RuntimeError("Login failed with response 401"))
    camera = ArgusCamera(settings(attempts=3), camera_factory=lambda _settings: fake)

    with pytest.raises(AuthenticationError):
        await camera.goto_preset(1)

    assert [call[0] for call in fake.calls].count("connect") == 1
    assert fake.closed


@pytest.mark.asyncio
async def test_connection_timeout_retries_with_new_camera_then_succeeds() -> None:
    first = FakeCamera(connect_error=TimeoutError("No Reolink P2P lookup reply"))
    second = FakeCamera()
    cameras = iter((first, second))
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    camera = ArgusCamera(settings(attempts=2), camera_factory=lambda _settings: next(cameras), sleep=sleep)
    await camera.goto_preset(1)

    assert first.closed
    assert second.closed
    assert delays == [0.01]


@pytest.mark.asyncio
async def test_ptz_rejection_closes_connection() -> None:
    fake = FakeCamera()
    fake.reply = Reply(ReplyHeader(400))
    camera = ArgusCamera(settings(), camera_factory=lambda _settings: fake)

    with pytest.raises(PtzRejectedError, match="response code 400"):
        await camera.goto_preset(1)

    assert fake.closed


@pytest.mark.asyncio
async def test_ptz_timeout_closes_connection() -> None:
    fake = FakeCamera()
    fake.command_error = TimeoutError("Timed out waiting for UDP Baichuan data")
    camera = ArgusCamera(settings(), camera_factory=lambda _settings: fake)

    with pytest.raises(PtzTimeoutError):
        await camera.goto_preset(1)

    assert fake.closed


@pytest.mark.asyncio
async def test_cleanup_after_protocol_exception() -> None:
    fake = FakeCamera()
    fake.command_error = RuntimeError("malformed response")
    camera = ArgusCamera(settings(), camera_factory=lambda _settings: fake)

    with pytest.raises(BaichuanProtocolError):
        await camera.goto_preset(1)

    assert fake.closed


@pytest.mark.asyncio
async def test_malformed_transport_is_not_a_preset_rejection() -> None:
    fake = FakeCamera()
    fake.command_error = ProtocolError("Short Baichuan header")
    camera = ArgusCamera(settings(), camera_factory=lambda _settings: fake)

    with pytest.raises(BaichuanProtocolError) as error:
        await camera.goto_preset(1)

    assert not isinstance(error.value, PtzRejectedError)
    assert fake.closed
    assert [call[0] for call in fake.calls].count("command") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("preset_id", [-1, 256, True, "3"])
async def test_invalid_preset_does_not_connect(preset_id: Any) -> None:
    fake = FakeCamera()
    camera = ArgusCamera(settings(), camera_factory=lambda _settings: fake)

    with pytest.raises(ConfigurationError):
        await camera.goto_preset(preset_id)

    assert fake.calls == []


@pytest.mark.asyncio
async def test_preset_archive_and_channel_are_preserved(tmp_path: Path) -> None:
    fake = FakeCamera()
    camera = ArgusCamera(replace(settings(), channel_id=7), camera_factory=lambda _settings: fake)
    output = tmp_path / "archive" / "prime.jpg"

    await camera.goto_preset(3, prime_snapshot_output=output)

    assert fake.calls[2] == ("snapshot", output, "main", False, 0)
    command = fake.calls[3]
    assert b"<channelId>7</channelId>" in command[2]
    assert b"<channelId>7</channelId>" in command[3]
    assert output.parent.is_dir()
    assert fake.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("timeout"), ProtocolError("Short Baichuan header")])
async def test_preset_list_failure_closes_without_retry(failure: Exception) -> None:
    fake = FakeCamera()
    fake.command_error = failure
    camera = ArgusCamera(settings(), camera_factory=lambda _settings: fake)

    with pytest.raises(BaichuanProtocolError):
        await camera.get_presets()

    assert fake.closed
    assert [call[0] for call in fake.calls] == ["connect", "login", "command", "close"]


@pytest.mark.asyncio
async def test_preset_list_rejection_closes_connection() -> None:
    fake = FakeCamera()
    fake.reply = Reply(ReplyHeader(400))
    camera = ArgusCamera(settings(), camera_factory=lambda _settings: fake)

    with pytest.raises(BaichuanProtocolError):
        await camera.get_presets()

    assert fake.closed
