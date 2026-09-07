"""One-shot asynchronous operations backed by PyNeolink."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from pyneolink import PtzPreset as PtzPreset  # type: ignore[import-untyped]
from pyneolink.core.bc import ProtocolError  # type: ignore[import-untyped]

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
from .protocol import validate_channel_id, validate_preset_id

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


class _Ptz(Protocol):
    def goto_preset(self, preset_id: int | str) -> None: ...

    def presets(self) -> tuple[PtzPreset, ...]: ...


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

    def ptz(self, *, channel_id: int | None = None) -> _Ptz: ...


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
        validate_preset_id(preset_id)
        validate_channel_id(self._settings.channel_id)
        log_event(self._logger, logging.INFO, "Moving to preset", preset_id=preset_id)

        def send(camera: BlockingCamera) -> None:
            self._prime_preset_session(camera, output=prime_snapshot_output)
            try:
                camera.ptz(channel_id=self._settings.channel_id).goto_preset(preset_id)
            except TimeoutError as error:
                raise PtzTimeoutError("preset response timed out") from error
            except PtzTimeoutError:
                raise
            except ProtocolError as error:
                # Upstream also uses ProtocolError for malformed transport headers.
                rejection = re.fullmatch(r"PTZ preset recall failed with response (\d+)", str(error))
                if rejection:
                    raise PtzRejectedError(
                        f"preset {preset_id} rejected with response code {rejection[1]}"
                    ) from error
                raise BaichuanProtocolError("preset command failed") from error
            except Exception as error:
                raise BaichuanProtocolError("preset command failed") from error
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
        validate_channel_id(self._settings.channel_id)

        def query(camera: BlockingCamera) -> tuple[PtzPreset, ...]:
            try:
                return camera.ptz(channel_id=self._settings.channel_id).presets()
            except TimeoutError as error:
                raise BaichuanProtocolError("preset-list response timed out") from error
            except Exception as error:
                raise BaichuanProtocolError("preset-list command failed") from error

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
    from pyneolink import Camera

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
