"""Explicit CLI updates from the accepted, signed installer publication."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from jsonschema import Draft202012Validator, ValidationError

from .build_identity import current_build
from .control_transport import open_https
from .runtime_identity import (
    installed_content_identity,
    verified_wheel_identity,
    wheel_content_identity,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE = re.compile(r"[0-9a-f]{40}\Z")
_GENERATION = _SHA256
_WHEEL = re.compile(
    r"vonk_cluster_profiles-[0-9]+\.[0-9]+\.[0-9]+-py3-none-any[.]whl\Z"
)
_NOTICE_ORIGIN = "https://install.vonkforge.ai"
_NOTICE_TTL_SECONDS = 86400
_NOTICE_FAILURE_RETRY_SECONDS = 900


def _embedded_public_key() -> Path:
    """The installer release signing key shipped inside the CLI itself."""

    packaged = files("cluster_profiles").joinpath("installer-release-public.pem")
    if packaged.is_file():
        return Path(str(packaged))
    return Path(__file__).resolve().parents[2] / "install/installer-release-public.pem"


INSTALLER_PUBLIC_KEY = _embedded_public_key()


class CliUpdateError(ValueError):
    """The bounded update attempt ended without confirmed installation."""


class CliUpdateIngressError(CliUpdateError):
    """Unverified ingress bytes or denied publication authentication."""


def configured_update_channel() -> str:
    """Return the configured accepted release channel for CLI updates."""

    channel = os.environ.get("VONK_CLI_UPDATE_CHANNEL", "stable").strip().lower()
    # An unrecognized value follows the stable channel rather than blocking.
    if channel not in ("stable", "dev"):
        channel = "stable"
    return channel


def _download(url: str, maximum: int, *, timeout: int = 20) -> bytes:
    """A total budget includes connect, headers, body and bounded retries."""
    request = urllib.request.Request(
        url, headers={"User-Agent": f"vonkctl/{current_build()['version']}"}
    )
    deadline = time.monotonic() + timeout
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with open_https(request, timeout=remaining) as response:
                content = response.read(maximum + 1)
            if not content:
                raise OSError("release response is not yet observable")
            if len(content) > maximum:
                raise CliUpdateIngressError("release object exceeds its byte bound")
            return content
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (401, 403) or 300 <= error.code < 400:
                raise CliUpdateIngressError(
                    "release download redirected or denied"
                ) from error
        except (OSError, urllib.error.URLError, TimeoutError):
            pass
        remaining = deadline - time.monotonic()
        if attempt < 2 and remaining > 0:
            time.sleep(min(0.25 * 2**attempt, remaining))
    raise CliUpdateError("release download outcome is unknown after its deadline")


def _verify(key: rsa.RSAPublicKey, content: bytes, signature: bytes) -> None:
    try:
        key.verify(signature, content, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError) as error:
        raise CliUpdateIngressError("release signature is invalid") from error


def _validate_release(release: object, release_raw: bytes) -> dict[str, object]:
    schema_resource = files("cluster_profiles").joinpath(
        "schemas/cli-release-projection.schema.json"
    )
    if not schema_resource.is_file():
        schema_resource = (
            Path(__file__).resolve().parent
            / "schemas/cli-release-projection.schema.json"
        )
    try:
        schema = json.loads(schema_resource.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CliUpdateError("current release schema is unavailable") from error
    try:
        Draft202012Validator(schema).validate(release)
        if (
            not isinstance(release, dict)
            or (
                json.dumps(release, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            != release_raw
        ):
            raise CliUpdateError("immutable release encoding is invalid")
    except ValidationError as error:
        raise CliUpdateError("immutable release schema is invalid") from error
    return release


def _signed_release(
    *,
    channel: str,
    public_key: Path,
    origin: str,
    download: Callable[[str, int], bytes],
) -> tuple[dict[str, object], str]:
    parsed = urllib.parse.urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise CliUpdateError("update origin must be an HTTPS origin")
    try:
        key_bytes = public_key.read_bytes()
        key = serialization.load_pem_public_key(key_bytes)
    except (OSError, ValueError) as error:
        raise CliUpdateIngressError(
            "installer signing public key is unavailable or invalid"
        ) from error
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size not in (3072, 4096):
        raise CliUpdateIngressError("installer signing public key is invalid")
    base = origin.rstrip("/")
    pointer = download(f"{base}/artifacts/{channel}/current.manifest", 64 * 1024)
    lines = pointer.splitlines(keepends=True)
    if len(lines) != 15 or not lines[-1].startswith(b"signature="):
        raise CliUpdateError("current release manifest is invalid")
    claims = b"".join(lines[:14])
    try:
        signature = base64.b64decode(
            lines[-1].removeprefix(b"signature=").strip(), validate=True
        )
    except ValueError as error:
        raise CliUpdateIngressError("current release signature is invalid") from error
    _verify(key, claims, signature)
    names = (
        "schema_version",
        "channel",
        "generation",
        "version",
        "source_sha",
        "expires_at",
        "release_path",
        "release_sha256",
        "release_signature_path",
        "release_signature_sha256",
        "nas_path",
        "nas_sha256",
        "spark_path",
        "spark_sha256",
    )
    fields: dict[str, str] = {}
    try:
        for name, line in zip(names, lines[:14], strict=True):
            key_name, value = line.decode("ascii").rstrip("\n").split("=", 1)
            if key_name != name:
                raise CliUpdateError("current release manifest is invalid")
            fields[name] = value
        if (
            re.fullmatch(r"[2-9]|[1-9][0-9]+", fields["schema_version"]) is None
            or fields["channel"] != channel
        ):
            raise CliUpdateError("current release manifest is invalid")
        if not _GENERATION.fullmatch(fields["generation"]) or not _SOURCE.fullmatch(
            fields["source_sha"]
        ):
            raise CliUpdateError("current release identity is invalid")
        if int(fields["expires_at"]) <= int(time.time()):
            raise CliUpdateError("current release manifest has expired")
        for name in ("release_sha256", "release_signature_sha256"):
            if not _SHA256.fullmatch(fields[name]):
                raise CliUpdateError("current release digest is invalid")
    except (UnicodeDecodeError, ValueError) as error:
        raise CliUpdateError("current release manifest is invalid") from error
    prefix = f"artifacts/{channel}/releases/{fields['generation']}"
    if (
        fields["release_path"] != f"{prefix}/release.json"
        or fields["release_signature_path"] != f"{prefix}/release.sig"
    ):
        raise CliUpdateError("immutable release path is invalid")
    release_raw = download(f"{base}/{fields['release_path']}", 1024 * 1024)
    release_sig = download(f"{base}/{fields['release_signature_path']}", 16 * 1024)
    if (
        hashlib.sha256(release_raw).hexdigest() != fields["release_sha256"]
        or hashlib.sha256(release_sig).hexdigest() != fields["release_signature_sha256"]
    ):
        raise CliUpdateIngressError("immutable release digest is invalid")
    try:
        _verify(key, release_raw, base64.b64decode(release_sig.strip(), validate=True))
        release = _validate_release(json.loads(release_raw), release_raw)
    except CliUpdateIngressError:
        raise
    except (ValueError, TypeError) as error:
        raise CliUpdateError("immutable release is not yet observable") from error
    if (
        not isinstance(release, dict)
        or any(release.get(name) != fields[name] for name in ("channel", "generation"))
        or release.get("schema_version") != int(fields["schema_version"])
    ):
        raise CliUpdateError("immutable release identity is inconsistent")
    artifacts = release.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CliUpdateError("immutable release artifacts are invalid")
    artifact = artifacts.get("cli-wheel")
    if not isinstance(artifact, dict):
        raise CliUpdateError("accepted release has no CLI wheel")
    path, digest, size = (
        artifact.get("path"),
        artifact.get("sha256"),
        artifact.get("size"),
    )
    suffix = path.removeprefix(f"{prefix}/cli/") if isinstance(path, str) else ""
    if (
        not isinstance(path, str)
        or not path.startswith(f"{prefix}/cli/")
        or not _WHEEL.fullmatch(suffix)
        or not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
        or type(size) is not int
        or not 0 < size <= 1024 * 1024 * 1024
    ):
        raise CliUpdateError("signed CLI wheel descriptor is invalid")
    return release, base


def run_update(
    *,
    channel: str,
    origin: str,
    apply: bool,
    public_key: Path | None = None,
    download: Callable[[str, int], bytes] = _download,
    compatibility_observation: Callable[[], object] | None = None,
    observation_timeout_seconds: float = 20,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    if (
        not math.isfinite(observation_timeout_seconds)
        or observation_timeout_seconds <= 0
    ):
        raise CliUpdateError("observation timeout must be finite and positive")
    if channel not in ("stable", "dev"):
        raise CliUpdateError("update channel must be stable or dev")
    for attempt in range(3):
        try:
            release, base = _signed_release(
                channel=channel,
                public_key=public_key or INSTALLER_PUBLIC_KEY,
                origin=origin,
                download=download,
            )
            break
        except CliUpdateIngressError:
            raise
        except CliUpdateError:
            if attempt == 2:
                raise
            sleeper(0.25 * 2**attempt)
    current = current_build()
    target_source = release["source_sha"]
    result: dict[str, object] = {
        "channel": channel,
        "current": current,
        "accepted_version": release["version"],
        "accepted_source_sha": target_source,
        "update_available": None,
        "updated": False,
    }
    artifacts = cast(dict[str, object], release["artifacts"])
    artifact = cast(dict[str, object], artifacts["cli-wheel"])
    path = cast(str, artifact["path"])
    digest = cast(str, artifact["sha256"])
    size = cast(int, artifact["size"])
    wheel = download(f"{base}/{path}", size)
    if len(wheel) != size or hashlib.sha256(wheel).hexdigest() != digest:
        raise CliUpdateIngressError("CLI wheel digest or size is invalid")
    for attempt in range(3):
        try:
            with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
                target_content = wheel_content_identity(archive)
                verified_wheel_identity(
                    archive,
                    source_sha=cast(str, target_source),
                    version=cast(str, release["version"]),
                )
            break
        except (KeyError, ValueError, zipfile.BadZipFile) as error:
            if attempt == 2:
                raise CliUpdateError(
                    "verified wheel archive remained unreadable"
                ) from error
            sleeper(0.25 * 2**attempt)
            wheel = download(f"{base}/{path}", size)
            if len(wheel) != size or hashlib.sha256(wheel).hexdigest() != digest:
                raise CliUpdateIngressError("CLI wheel digest or size is invalid")
    installed_content = installed_content_identity()
    equal_content = (
        target_content is not None
        and installed_content is not None
        and installed_content == target_content
    )
    result["update_available"] = (
        not equal_content
        if target_content is not None and installed_content is not None
        else None
    )
    if not apply or equal_content:
        return result
    from .control_client import (
        ControlClient,
        ControlClientError,
        ControlForbidden,
        ControlUnauthorized,
    )

    # This is an authority observation, not a second compatibility planner.
    # Signed wheel ingress already verified the exact installed bytes. Source
    # revisions and worker bookkeeping cannot veto that accepted publication.
    deadline = clock() + observation_timeout_seconds
    observed = None
    while clock() < deadline:
        try:
            deployed = (
                compatibility_observation()
                if compatibility_observation is not None
                else ControlClient.from_environment().request(
                    "GET",
                    "/api/cli/contract",
                    timeout_seconds=max(0.001, deadline - clock()),
                )
            )
            installed_schema = json.loads(
                files("cluster_profiles")
                .joinpath("schemas/cli-update-contract.schema.json")
                .read_text()
            )
            Draft202012Validator(installed_schema).validate(deployed)
            from .generated_control.models.cli_update_contract import CliUpdateContract

            if not isinstance(deployed, dict):
                raise TypeError("controller compatibility document is not an object")
            observed = CliUpdateContract.from_dict(deployed)
        except (ControlUnauthorized, ControlForbidden):
            raise
        except (
            ControlClientError,
            OSError,
            KeyError,
            TypeError,
            ValueError,
            ValidationError,
        ):
            remaining = deadline - clock()
            if remaining > 0:
                sleeper(min(0.5, remaining))
            continue
        result["controller"] = deployed
        result["compatibility"] = observed.worker_compatibility
        break
    if observed is None:
        # An unresolved apply must not be rendered as an unchanged success.
        raise CliUpdateError(
            "controller observation remained unknown within the apply deadline"
        )
    uv = None
    for attempt in range(3):
        uv = shutil.which("uv")
        if uv is not None:
            break
        sleeper(0.25 * 2**attempt)
    if uv is None:
        raise CliUpdateError(
            "verified CLI installer was not observed within its retry bound"
        )
    with tempfile.TemporaryDirectory(prefix="vonkctl-update-") as directory:
        wheel_path = Path(directory) / path.rsplit("/", 1)[-1]
        wheel_path.write_bytes(wheel)
        # Same form as the installer's first install: uv resolves the wheel's
        # own dependencies (a release may add or raise one) and its tool
        # receipt records this release rather than an older one.
        command = [
            uv,
            "tool",
            "install",
            "--force",
            "--python",
            "3.14",
            str(wheel_path),
        ]
        install_deadline = time.monotonic() + 180
        for attempt in range(3):
            remaining = install_deadline - time.monotonic()
            if remaining <= 0:
                raise CliUpdateError("verified CLI installation deadline elapsed")
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=remaining,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            if completed is not None and completed.returncode == 0:
                break
            if attempt == 2:
                raise CliUpdateError(
                    "verified CLI installation outcome remained unknown"
                )
            time.sleep(
                min(0.25 * 2**attempt, max(0, install_deadline - time.monotonic()))
            )
    result["previous"] = current
    result["current"] = {
        "version": release["version"],
        "source_sha": target_source,
    }
    result["updated"] = True
    result["update_available"] = False
    return result


def _notice_context(
    public_key: Path | None = None,
) -> tuple[Path, Path, str, str] | None:
    try:
        channel = configured_update_channel()
    except CliUpdateError:
        return None
    public_key = public_key or INSTALLER_PUBLIC_KEY
    try:
        descriptor = os.open(public_key, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            key_stat = os.fstat(descriptor)
            if not stat.S_ISREG(key_stat.st_mode) or not 0 < key_stat.st_size <= 16384:
                return None
            key_bytes = os.read(descriptor, 16385)
            if len(key_bytes) != key_stat.st_size:
                return None
        finally:
            os.close(descriptor)
        key_digest = hashlib.sha256(key_bytes).hexdigest()
        configured_cache = os.environ.get("XDG_CACHE_HOME")
        cache_root = (
            Path(configured_cache) if configured_cache else Path.home() / ".cache"
        )
        if not cache_root.is_absolute():
            return None
    except (OSError, ValueError):
        return None
    return (
        cache_root / "vonkctl" / "update-notice.json",
        public_key,
        key_digest,
        channel,
    )


def _notice_record(
    *, key_digest: str, channel: str, verified: bool, available: bool
) -> dict[str, object]:
    current = current_build()
    return {
        "checked_at": int(time.time()),
        "verified": verified,
        "channel": channel,
        "origin": _NOTICE_ORIGIN,
        "update_available": available,
        "source_sha": current["source_sha"],
        "version": current["version"],
        "key_sha256": key_digest,
    }


def _read_notice(cache: Path) -> dict[str, object] | None:
    try:
        descriptor = os.open(cache, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode) or observed.st_size > 4096:
                return None
            raw = os.read(descriptor, 4097)
            if len(raw) != observed.st_size:
                return None
        finally:
            os.close(descriptor)
        stored = json.loads(raw)
    except (OSError, ValueError):
        return None
    return stored if isinstance(stored, dict) else None


def _fresh_notice(
    stored: dict[str, object] | None, key_digest: str, channel: str
) -> bool:
    if stored is None:
        return False
    checked_at = stored.get("checked_at")
    if (
        type(checked_at) is not int
        or stored.get("key_sha256") != key_digest
        or stored.get("channel") != channel
        or stored.get("origin") != _NOTICE_ORIGIN
        or type(stored.get("verified")) is not bool
    ):
        return False
    age = int(time.time()) - checked_at
    ttl = (
        _NOTICE_TTL_SECONDS
        if stored["verified"] is True
        else _NOTICE_FAILURE_RETRY_SECONDS
    )
    return 0 <= age < ttl


def _write_notice(cache: Path, record: dict[str, object]) -> None:
    try:
        cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=cache.parent, prefix=".update-notice-", delete=False
        ) as handle:
            json.dump(record, handle)
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        temporary.replace(cache)
    except OSError:
        pass


def interactive_notice() -> str | None:
    """Read a locally cached signed result without doing network I/O."""

    context = _notice_context()
    if context is None:
        return None
    cache, _, key_digest, channel = context
    stored = _read_notice(cache)
    if (
        _fresh_notice(stored, key_digest, channel)
        and stored is not None
        and stored["verified"] is True
        and stored.get("update_available") is True
    ):
        return "Accepted vonkctl update available."
    return None


def cache_update_notice(
    result: dict[str, object],
    *,
    public_key: Path | None = None,
    origin: str = _NOTICE_ORIGIN,
) -> None:
    """Cache an explicit signed stable-channel check for interactive commands."""

    if origin != _NOTICE_ORIGIN:
        return
    context = _notice_context(public_key)
    if context is None:
        return
    cache, _, key_digest, channel = context
    if result.get("channel") != channel:
        return
    _write_notice(
        cache,
        _notice_record(
            key_digest=key_digest,
            channel=channel,
            verified=True,
            available=result.get("update_available") is True,
        ),
    )


def begin_interactive_update_check() -> None:
    """Start one short-lived signed check when the current cache is stale."""

    context = _notice_context()
    if context is None:
        return
    cache, _, key_digest, channel = context
    if _fresh_notice(_read_notice(cache), key_digest, channel):
        return
    descriptor = _notice_lock(cache)
    if descriptor is None:
        return
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cluster_profiles.cli_update",
                "--background-notice",
                str(descriptor),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(descriptor,),
            start_new_session=True,
        )
    except (OSError, ValueError):
        pass
    finally:
        os.close(descriptor)


def _notice_lock(cache: Path) -> int | None:
    """The kernel fences the owner; the stable pathname is never unlinked."""
    descriptor = None
    try:
        cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            cache.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        return None


def background_notice_check(
    *,
    download: Callable[[str, int], bytes] = _download,
    lock_descriptor: int | None = None,
) -> None:
    """Verify within the owning process's alarm, releasing only its descriptor."""
    context = _notice_context()
    if context is None:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        return
    cache, public_key, key_digest, channel = context
    descriptor = lock_descriptor if lock_descriptor is not None else _notice_lock(cache)
    if descriptor is None:
        return
    try:
        try:
            result = run_update(
                channel=channel,
                public_key=public_key,
                origin=_NOTICE_ORIGIN,
                apply=False,
                download=download,
            )
        except (CliUpdateError, OSError, TimeoutError):
            _write_notice(
                cache,
                _notice_record(
                    key_digest=key_digest,
                    channel=channel,
                    verified=False,
                    available=False,
                ),
            )
        else:
            cache_update_notice(result, public_key=public_key)
    finally:
        os.close(descriptor)


def _notice_timeout(_signum: int, _frame: object) -> None:
    raise TimeoutError("background release check timed out")


def _background_main() -> int:
    if len(sys.argv) not in (2, 3) or sys.argv[1] != "--background-notice":
        return 2
    descriptor = int(sys.argv[2]) if len(sys.argv) == 3 else None
    signal.signal(signal.SIGALRM, _notice_timeout)
    signal.alarm(20)
    try:
        background_notice_check(
            download=lambda url, maximum: _download(url, maximum, timeout=5),
            lock_descriptor=descriptor,
        )
    finally:
        signal.alarm(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(_background_main())
