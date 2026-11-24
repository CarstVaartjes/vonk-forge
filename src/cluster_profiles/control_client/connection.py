"""Where vonkctl finds its Controller URL and token: the login files, or env paths.

``vonkctl auth login`` writes the token to ``$XDG_CONFIG_HOME/vonkctl/token`` and
the URL plus the Controller-published expiry to ``connection.json`` beside it.
``VONK_CONTROL_URL`` and ``VONK_CONTROL_TOKEN_FILE`` override the same two
places, so there is one reader (``_read_token_file``) and one token shape. The
stored expiry is a local copy for warnings only: it never refuses a command.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cluster_profiles.control_client.errors import (
    ControlClientError,
    ControlCredentialScope,
    ControlNotConfigured,
    LocalFilesystemError,
    LocalInputError,
)

SCHEMA_VERSION = 1
WARN_DAYS = 7
URL_ENV = "VONK_CONTROL_URL"
TOKEN_FILE_ENV = "VONK_CONTROL_TOKEN_FILE"
_MAX_CONNECTION_BYTES = 4096
_CONNECTION_KEYS = {"schema_version", "controller_url", "token_expires_at"}


def validated_origin(base_url: str) -> str:
    """The HTTPS origin without credentials, as one string without a trailing slash."""

    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ControlClientError(
            "control URL must be an HTTPS origin without credentials"
        )
    return base_url.rstrip("/")


def config_directory() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    root = Path(configured) if configured else Path.home() / ".config"
    return root / "vonkctl"


def login_token_path() -> Path:
    return config_directory() / "token"


def connection_path() -> Path:
    return config_directory() / "connection.json"


def parse_expiry(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("expiry has no time zone")
    return parsed.astimezone(UTC)


def format_expiry(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class StoredConnection:
    controller_url: str
    token_expires_at: datetime

    def document(self) -> bytes:
        return (
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "controller_url": self.controller_url,
                    "token_expires_at": format_expiry(self.token_expires_at),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode()


def read_connection() -> StoredConnection | None:
    """The login's connection document; None before the first login."""

    path = connection_path()
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CONNECTION_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise LocalFilesystemError(
            "vonkctl connection file cannot be read; run vonkctl auth login"
        ) from None
    try:
        if len(raw) > _MAX_CONNECTION_BYTES:
            raise ValueError("too large")
        document = json.loads(raw)
        if (
            not isinstance(document, dict)
            or set(document) != _CONNECTION_KEYS
            or document["schema_version"] != SCHEMA_VERSION
            or not isinstance(document["controller_url"], str)
            or not isinstance(document["token_expires_at"], str)
        ):
            raise ValueError("unexpected shape")
        return StoredConnection(
            validated_origin(document["controller_url"]),
            parse_expiry(document["token_expires_at"]),
        )
    except ValueError, ControlClientError:
        raise LocalFilesystemError(
            "vonkctl connection file is invalid; run vonkctl auth login"
        ) from None


def write_private(path: Path, content: bytes) -> None:
    """Replace ``path`` atomically as a 0600 file in a 0700 directory."""

    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    directory_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


@dataclass(frozen=True)
class Credential:
    """What one command connects with, and where each part came from."""

    url: str
    token_file: Path
    url_from_environment: bool
    token_from_environment: bool
    expires_at: datetime | None

    @property
    def from_login(self) -> bool:
        return not self.token_from_environment


def read_connection_or_none() -> StoredConnection | None:
    """The stored connection, or None when absent or unreadable (login rewrites it)."""

    try:
        return read_connection()
    except LocalInputError:
        return None


def days_until(moment: datetime, now: datetime) -> int:
    """Whole days left, rounded up: a fresh 30-day token reads 30."""

    return max(0, math.ceil((moment - now).total_seconds() / 86400))


def resolve_credential() -> Credential:
    url = os.environ.get(URL_ENV, "")
    token = os.environ.get(TOKEN_FILE_ENV, "")
    stored: StoredConnection | None = None
    if not token and url:
        if login_token_path().exists():
            # The login's bearer is for the Controller it was minted by; an
            # unknown or different owner fails closed (login rewrites the file).
            stored = read_connection_or_none()
            if stored is None or url.rstrip("/") != stored.controller_url:
                raise ControlCredentialScope
    elif not token or not url:
        stored = read_connection()
    if not url and stored is not None:
        url = stored.controller_url
    token_file = Path(token) if token else login_token_path()
    if not url or (not token and not token_file.exists()):
        raise ControlNotConfigured
    return Credential(
        url=url,
        token_file=token_file,
        url_from_environment=bool(os.environ.get(URL_ENV)),
        token_from_environment=bool(token),
        expires_at=stored.token_expires_at if stored and not token else None,
    )


def expiry_warning(credential: Credential, now: datetime) -> str | None:
    """The one stderr line for a login token near or past its stored expiry."""

    if not credential.from_login or credential.expires_at is None:
        return None
    left = credential.expires_at - now
    when = format_expiry(credential.expires_at)
    if left <= timedelta(0):
        return (
            f"Controller token expired on {when} (the Controller decides); "
            "run vonkctl auth login"
        )
    if left <= timedelta(days=WARN_DAYS):
        days = days_until(credential.expires_at, now)
        return (
            f"Controller token expires in {days} "
            f"day{'s' if days != 1 else ''} ({when}); run vonkctl auth login"
        )
    return None
