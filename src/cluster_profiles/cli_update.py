"""Explicit CLI updates from the accepted, signed installer publication."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
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

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE = re.compile(r"[0-9a-f]{40}\Z")
_GENERATION = _SHA256
_WHEEL = re.compile(
    r"vonk_cluster_profiles-[0-9]+\.[0-9]+\.[0-9]+-py3-none-any[.]whl\Z"
)
_NOTICE_ORIGIN = "https://install.vonkforge.ai"
_NOTICE_TTL_SECONDS = 86400
_NOTICE_FAILURE_RETRY_SECONDS = 900
_NOTICE_LOCK_STALE_SECONDS = 30


class CliUpdateError(ValueError):
    """The requested CLI update cannot be safely verified or installed."""


def configured_update_channel() -> str:
    """Return the configured accepted release channel for CLI updates."""

    channel = os.environ.get("VONK_CLI_UPDATE_CHANNEL", "stable")
    if channel not in ("stable", "dev"):
        raise CliUpdateError("VONK_CLI_UPDATE_CHANNEL must be stable or dev")
    return channel


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise CliUpdateError("release download redirected")


def _download(url: str, maximum: int, *, timeout: int = 20) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": f"vonkctl/{current_build()['version']}"}
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=timeout
        ) as response:
            if response.status != 200:
                raise CliUpdateError("release download failed")
            content = response.read(maximum + 1)
    except OSError as error:
        raise CliUpdateError("release download failed") from error
    if not content or len(content) > maximum:
        raise CliUpdateError("release object is empty or too large")
    return content


def _verify(key: rsa.RSAPublicKey, content: bytes, signature: bytes) -> None:
    try:
        key.verify(signature, content, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError) as error:
        raise CliUpdateError("release signature is invalid") from error


def _validate_release(release: object, release_raw: bytes) -> dict[str, object]:
    schema_resource = files("cluster_profiles").joinpath(
        "schemas/install-release-manifest.schema.json"
    )
    if not schema_resource.is_file():
        schema_resource = (
            Path(__file__).resolve().parents[2]
            / "schemas/install-release-manifest.schema.json"
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
        raise CliUpdateError(
            "installer signing public key is unavailable or invalid"
        ) from error
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size not in (3072, 4096):
        raise CliUpdateError("installer signing public key is invalid")
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
        raise CliUpdateError("current release signature is invalid") from error
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
        if fields["schema_version"] != "2" or fields["channel"] != channel:
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
        raise CliUpdateError("immutable release digest is invalid")
    try:
        _verify(key, release_raw, base64.b64decode(release_sig.strip(), validate=True))
        release = _validate_release(json.loads(release_raw), release_raw)
    except (ValueError, TypeError) as error:
        raise CliUpdateError("immutable release is invalid") from error
    if (
        not isinstance(release, dict)
        or any(
            release.get(name) != fields[name]
            for name in ("channel", "generation", "version", "source_sha")
        )
        or release.get("schema_version") != 2
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
    public_key: Path,
    origin: str,
    apply: bool,
    download: Callable[[str, int], bytes] = _download,
) -> dict[str, object]:
    release, base = _signed_release(
        channel=channel, public_key=public_key, origin=origin, download=download
    )
    current = current_build()
    target_source = release["source_sha"]
    changed = (
        current["source_sha"] != target_source
        or current["version"] != release["version"]
    )
    result: dict[str, object] = {
        "channel": channel,
        "current": current,
        "accepted_version": release["version"],
        "accepted_source_sha": target_source,
        "update_available": changed,
        "updated": False,
    }
    if not apply or not changed:
        return result
    artifacts = cast(dict[str, object], release["artifacts"])
    artifact = cast(dict[str, object], artifacts["cli-wheel"])
    path = cast(str, artifact["path"])
    digest = cast(str, artifact["sha256"])
    size = cast(int, artifact["size"])
    wheel = download(f"{base}/{path}", size)
    if len(wheel) != size or hashlib.sha256(wheel).hexdigest() != digest:
        raise CliUpdateError("CLI wheel digest or size is invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
            identity = json.loads(archive.read("cluster_profiles/build-identity.json"))
        if identity != {
            "schema_version": 1,
            "source_sha": target_source,
            "release_version": release["version"],
        }:
            raise CliUpdateError("CLI wheel identity does not match accepted release")
    except (KeyError, ValueError, zipfile.BadZipFile) as error:
        raise CliUpdateError("CLI wheel identity is invalid") from error
    interpreter = Path(sys.executable)
    if not (interpreter.parent.parent / "pyvenv.cfg").is_file():
        raise CliUpdateError(
            "CLI update requires a writable Python virtual environment"
        )
    uv = shutil.which("uv")
    if uv is None:
        raise CliUpdateError("CLI update requires uv")
    with tempfile.TemporaryDirectory(prefix="vonkctl-update-") as directory:
        wheel_path = Path(directory) / path.rsplit("/", 1)[-1]
        wheel_path.write_bytes(wheel)
        command = [
            uv,
            "pip",
            "install",
            "--python",
            str(interpreter),
            "--no-deps",
            "--reinstall",
            "--offline",
            str(wheel_path),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=180, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CliUpdateError(
                "CLI installation failed in the current Python environment"
            ) from error
        if completed.returncode != 0:
            raise CliUpdateError(
                "CLI installation failed in the current Python environment"
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
    if os.environ.get("VONK_CLI_UPDATE_NOTICES") != "1":
        return None
    try:
        channel = configured_update_channel()
    except CliUpdateError:
        return None
    if public_key is None:
        configured = os.environ.get("VONK_INSTALLER_PUBLIC_KEY_FILE")
        if not configured:
            return None
        public_key = Path(configured)
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
    current = current_build()
    if (
        type(checked_at) is not int
        or stored.get("key_sha256") != key_digest
        or stored.get("channel") != channel
        or stored.get("origin") != _NOTICE_ORIGIN
        or stored.get("source_sha") != current["source_sha"]
        or stored.get("version") != current["version"]
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
        return (
            "Accepted vonkctl update available; run "
            f"'vonkctl update --channel {channel} --apply' to install it."
        )
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
    lock = cache.with_suffix(".lock")
    created = False
    try:
        cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            observed = lock.lstat()
            age = time.time() - observed.st_mtime
            if not stat.S_ISREG(observed.st_mode) or age < _NOTICE_LOCK_STALE_SECONDS:
                return
            lock.unlink()
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.close(descriptor)
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cluster_profiles.cli_update",
                "--background-notice",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        if created:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass


def background_notice_check(
    *, download: Callable[[str, int], bytes] = _download
) -> None:
    """Verify the accepted publication in the detached one-shot process."""

    context = _notice_context()
    if context is None:
        return
    cache, public_key, key_digest, channel = context
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
        try:
            cache.with_suffix(".lock").unlink(missing_ok=True)
        except OSError:
            pass


def _notice_timeout(_signum: int, _frame: object) -> None:
    raise TimeoutError("background release check timed out")


def _background_main() -> int:
    if sys.argv[1:] != ["--background-notice"]:
        return 2
    signal.signal(signal.SIGALRM, _notice_timeout)
    signal.alarm(20)
    try:
        background_notice_check(
            download=lambda url, maximum: _download(url, maximum, timeout=5)
        )
    finally:
        signal.alarm(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(_background_main())
