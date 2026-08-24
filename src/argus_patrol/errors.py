"""Stable, actionable errors for the CLI and scheduler."""


class ArgusError(RuntimeError):
    """Base error for an Argus operation."""


class ConfigurationError(ArgusError):
    """Missing or invalid local configuration."""


class UidLookupError(ArgusError):
    """Reolink P2P servers did not resolve the UID."""


class P2pRegistrationError(ArgusError):
    """UID lookup worked but P2P registration did not."""


class RelayError(ArgusError):
    """No direct, mapped, or relay P2P candidate connected."""


class CameraWakeTimeout(ArgusError):
    """Transport connected but the sleeping camera did not complete login."""


class AuthenticationError(ArgusError):
    """Camera rejected credentials. This must not be retried automatically."""


class BaichuanProtocolError(ArgusError):
    """Baichuan framing or encryption exchange failed."""


class PtzRejectedError(ArgusError):
    """Camera replied to preset recall with a non-success response code."""


class PtzTimeoutError(ArgusError):
    """Preset request was sent but no matching response arrived."""
