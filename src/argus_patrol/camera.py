"""One-shot asynchronous operations backed by PyNeolink."""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from .errors import (
    AuthenticationError,
    BaichuanProtocolError,
    CameraWakeTimeout,
    P2pRegistrationError,
    PtzRejectedError,
    PtzTimeoutError,
    RelayError,
    UidLookupError,
)
from .logging import log_event
from .protocol import (
    PTZ_PRESET_COMMAND_ID,
    PTZ_PRESET_LIST_COMMAND_ID,
    build_goto_preset_payload,
    build_preset_extension,
)

T = TypeVar("T")


@dataclass(frozen=True)
class CameraSettings:
    """Connection settings for a single UID-addressed battery camera."""

    uid: str
    username: str
    password: str
    channel_id: int = 0
    connect_timeout_seconds: float = 12.0
    connect_attempts: int = 3
    retry_backoff_seconds: float = 2.0


@dataclass(frozen=True)
class PtzPreset:
    """One stored, device-reported PTZ preset slot."""

    id: int
    name: str | None
    enabled: bool | None


class _ResponseHeader(Protocol):
    response_code: int


class _Response(Protocol):
    @property
    def header(self) -> _ResponseHeader: ...

    @property
    def payload(self) -> bytes: ...


class BlockingCamera(Protocol):
    """Subset of PyNeolink's synchronous Camera used by this project."""

    connected_address: tuple[str, int] | None

    def connect(self) -> None: ...

    def login(self) -> str: ...

    def close(self) -> None: ...

    def info(self, *, include_sensitive: bool = False) -> dict[str, Any]: ...

    def snapshot(
        self,
        *,
        out: str | Path | None = None,
        stream_type: str = "main",
        retry_on_timeout: bool = True,
        reconnect_retries: int = 1,
    ) -> bytes | Path: ...

    def command(
        self,
        msg_id: int,
        payload: bytes = b"",
        *,
        extension: bytes = b"",
        retry_on_timeout: bool = True,
        reconnect_retries: int = 1,
    ) -> _Response: ...


CameraFactory = Callable[[CameraSettings], BlockingCamera]
AsyncSleep = Callable[[float], Awaitable[None]]


class ArgusCamera:
    """Run isolated, battery-conscious camera operations.

    A fresh PyNeolink client is constructed for every public operation and is
    closed in all paths. ``asyncio.to_thread`` keeps PyNeolink's blocking socket
    work off the caller's event loop.
    """

    def __init__(
        self,
        settings: CameraSettings,
        *,
        camera_factory: CameraFactory | None = None,
        sleep: AsyncSleep = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._camera_factory = camera_factory or _pyneolink_camera
        self._sleep = sleep
        self._logger = logger or logging.getLogger("argus_patrol")

    async def status(self) -> dict[str, Any]:
        """Get login-derived camera status and disconnect immediately."""
        return await self._operate(lambda camera: camera.info())

    async def snapshot(self, output: Path) -> Path:
        """Save one snapshot without starting a continuous video stream."""

        def capture(camera: BlockingCamera) -> Path:
            try:
                result = camera.snapshot(out=output, retry_on_timeout=False, reconnect_retries=0)
            except TimeoutError as error:
                raise BaichuanProtocolError("snapshot response timed out") from error
            except BaichuanProtocolError:
                raise
            except Exception as error:
                raise BaichuanProtocolError("snapshot command failed") from error
            if not isinstance(result, Path):
                raise BaichuanProtocolError("camera returned snapshot bytes despite an output path")
            return result

        saved = await self._operate(capture)
        log_event(self._logger, logging.INFO, "Snapshot saved", output=saved)
        return saved

    async def goto_preset(self, preset_id: int, *, prime_snapshot_output: Path | None = None) -> None:
        """Prime the Argus control session, recall one preset, then disconnect."""
        payload = build_goto_preset_payload(preset_id, self._settings.channel_id)
        extension = build_preset_extension(self._settings.channel_id)
        log_event(self._logger, logging.INFO, "Moving to preset", preset_id=preset_id)

        def send(camera: BlockingCamera) -> None:
            self._prime_preset_session(camera, output=prime_snapshot_output)
            try:
                reply = camera.command(
                    PTZ_PRESET_COMMAND_ID,
                    payload,
                    extension=extension,
                    # No retry after send: arrival may be unknown and duplicate PTZ
                    # recalls add wake time without making the operation safer.
                    retry_on_timeout=False,
                    reconnect_retries=0,
                )
            except TimeoutError as error:
                raise PtzTimeoutError("preset response timed out") from error
            except PtzTimeoutError:
                raise
            except Exception as error:
                raise BaichuanProtocolError("preset command failed") from error
            response_code = reply.header.response_code
            log_event(
                self._logger,
                logging.DEBUG,
                "Preset response",
                response_code=response_code,
                payload=_debug_payload(reply.payload),
            )
            if response_code != 200:
                raise PtzRejectedError(f"preset {preset_id} rejected with response code {response_code}")
            log_event(self._logger, logging.INFO, "Preset accepted", preset_id=preset_id)

        await self._operate(send)

    def _prime_preset_session(self, camera: BlockingCamera, *, output: Path | None = None) -> None:
        """Issue the snapshot required before Argus PT preset recall.

        The target Argus PT Ultra returns command-19/400 immediately after
        login, but accepts it after Baichuan's snapshot exchange on the same
        session. This does not start a continuous stream. Patrol archive mode
        optionally saves this already-required image to ``output``.
        """
        try:
            if output is not None:
                output.parent.mkdir(parents=True, exist_ok=True)
                snapshot = camera.snapshot(
                    out=output,
                    retry_on_timeout=False,
                    reconnect_retries=0,
                )
            else:
                snapshot = camera.snapshot(retry_on_timeout=False, reconnect_retries=0)
        except TimeoutError as error:
            raise PtzTimeoutError("PTZ session-priming snapshot timed out") from error
        except Exception as error:
            raise BaichuanProtocolError("PTZ session-priming snapshot failed") from error
        if output is not None:
            if not isinstance(snapshot, Path):
                raise BaichuanProtocolError("PTZ session-priming snapshot was not saved")
            log_event(self._logger, logging.INFO, "Patrol snapshot saved", output=snapshot)
            return
        if not isinstance(snapshot, bytes):
            raise BaichuanProtocolError("PTZ session-priming snapshot did not return image bytes")
        log_event(self._logger, logging.DEBUG, "PTZ session primed", snapshot_bytes=len(snapshot))

    async def get_presets(self) -> tuple[PtzPreset, ...]:
        """Read stored preset IDs and names without moving the camera."""
        extension = build_preset_extension(self._settings.channel_id)

        def query(camera: BlockingCamera) -> tuple[PtzPreset, ...]:
            try:
                reply = camera.command(
                    PTZ_PRESET_LIST_COMMAND_ID,
                    extension=extension,
                    retry_on_timeout=False,
                    reconnect_retries=0,
                )
            except TimeoutError as error:
                raise BaichuanProtocolError("preset-list response timed out") from error
            except Exception as error:
                raise BaichuanProtocolError("preset-list command failed") from error
            if reply.header.response_code != 200:
                raise BaichuanProtocolError(
                    f"preset-list request rejected with response code {reply.header.response_code}"
                )
            return _parse_preset_list(reply.payload)

        presets = await self._operate(query)
        log_event(self._logger, logging.INFO, "Preset list read", count=len(presets))
        return presets

    async def _operate(self, operation: Callable[[BlockingCamera], T]) -> T:
        """Connect, login, run one operation, and close the session."""
        last_error: Exception | None = None
        for attempt in range(1, self._settings.connect_attempts + 1):
            camera = self._camera_factory(self._settings)
            log_event(
                self._logger,
                logging.INFO,
                "Connecting to Argus...",
                attempt=attempt,
                attempts=self._settings.connect_attempts,
            )
            try:
                await asyncio.to_thread(camera.connect)
            except Exception as error:
                await self._close_quietly(camera)
                classified = _connection_error(error)
                last_error = classified
                if attempt == self._settings.connect_attempts:
                    raise classified from error
                delay = self._settings.retry_backoff_seconds * (2 ** (attempt - 1))
                log_event(
                    self._logger,
                    logging.WARNING,
                    "Connection attempt failed; retrying",
                    error=type(classified).__name__,
                    delay_seconds=delay,
                )
                await self._sleep(delay)
                continue

            try:
                await asyncio.to_thread(camera.login)
            except Exception as error:
                await self._close_quietly(camera)
                raise _login_error(error) from error

            address = camera.connected_address
            log_event(
                self._logger,
                logging.INFO,
                "Authenticated",
                transport="UID/P2P",
                address=f"{address[0]}:{address[1]}" if address else "unknown",
            )
            try:
                return await asyncio.to_thread(operation, camera)
            finally:
                await self._close_quietly(camera)
                log_event(self._logger, logging.INFO, "Disconnected")

        assert last_error is not None
        raise last_error

    async def _close_quietly(self, camera: BlockingCamera) -> None:
        try:
            await asyncio.to_thread(camera.close)
        except Exception as error:
            log_event(self._logger, logging.DEBUG, "Disconnect failed", error=type(error).__name__)


def _pyneolink_camera(settings: CameraSettings) -> BlockingCamera:
    """Build a PyNeolink camera without its optional on-disk state cache."""
    from pyneolink import Camera  # type: ignore[import-untyped]

    # ``relay`` invokes PyNeolink's remote P2P registration flow. Its registered
    # candidates still include direct/local and mapped UDP before relay.
    return cast(
        BlockingCamera,
        Camera(
            uid=settings.uid,
            username=settings.username,
            password=settings.password,
            discovery="relay",
            channel_id=settings.channel_id,
            timeout=settings.connect_timeout_seconds,
            state_path=None,
        ),
    )


def _connection_error(error: Exception) -> UidLookupError | P2pRegistrationError | RelayError:
    message = str(error).lower()
    if "p2p lookup" in message or "lookup reply" in message or "register/relay servers" in message:
        return UidLookupError("UID lookup failed")
    if "register" in message or "connection details" in message:
        return P2pRegistrationError("P2P registration failed")
    return RelayError("P2P direct/relay connection failed")


def _login_error(error: Exception) -> AuthenticationError | CameraWakeTimeout | BaichuanProtocolError:
    message = str(error).lower()
    if "login failed" in message or "login nonce" in message:
        return AuthenticationError("camera authentication failed")
    if isinstance(error, TimeoutError) or "timed out" in message:
        return CameraWakeTimeout("camera wake/connect timed out during login")
    return BaichuanProtocolError("Baichuan login failed")


def _parse_preset_list(payload: bytes) -> tuple[PtzPreset, ...]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise BaichuanProtocolError("preset-list response is not XML") from error

    presets: list[PtzPreset] = []
    for preset in root.findall(".//presetList/preset"):
        raw_id = preset.findtext("id")
        if raw_id is None:
            continue
        try:
            preset_id = int(raw_id)
        except ValueError as error:
            raise BaichuanProtocolError("preset-list response contains a non-integer ID") from error
        raw_enabled = preset.findtext("enable")
        enabled = None if raw_enabled is None else raw_enabled.strip() not in {"0", "false", "False"}
        name = preset.findtext("name")
        presets.append(PtzPreset(id=preset_id, name=name, enabled=enabled))
    return tuple(presets)


def _debug_payload(payload: bytes) -> str:
    """Bound one decrypted PTZ response for ``--debug`` without dumping frames."""
    text = " ".join(payload.decode("utf-8", errors="replace").split())
    return text[:512] if text else "<empty>"
