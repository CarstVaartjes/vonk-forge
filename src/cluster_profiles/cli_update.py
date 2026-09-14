"""Explicit CLI updates from the accepted, signed installer publication."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
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


class CliUpdateError(ValueError):
    """The requested CLI update cannot be safely verified or installed."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise CliUpdateError("release download redirected")


def _download(url: str, maximum: int) -> bytes:
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
            url, timeout=20
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
    result["updated"] = True
    result["update_available"] = False
    return result


def interactive_notice() -> str | None:
    """Return a cached notice from an explicit update check without network I/O."""

    if os.environ.get("VONK_CLI_UPDATE_NOTICES") != "1":
        return None
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    cache = cache_root / "vonkctl" / "update-notice.json"
    now = int(time.time())
    try:
        stored = json.loads(cache.read_text())
    except (OSError, ValueError):
        return None
    if (
        isinstance(stored, dict)
        and type(stored.get("checked_at")) is int
        and 0 <= now - stored["checked_at"] < 86400
        and stored.get("update_available") is True
        and stored.get("source_sha") == current_build()["source_sha"]
    ):
        return "Accepted vonkctl update available; run 'vonkctl update --apply' to install it."
    return None


def cache_update_notice(result: dict[str, object]) -> None:
    """Cache an explicit stable-channel check for later interactive display."""

    if (
        os.environ.get("VONK_CLI_UPDATE_NOTICES") != "1"
        or result.get("channel") != "stable"
    ):
        return
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    cache = cache_root / "vonkctl" / "update-notice.json"
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=cache.parent, prefix=".update-notice-", delete=False
        ) as handle:
            json.dump(
                {
                    "checked_at": int(time.time()),
                    "update_available": result.get("update_available") is True,
                    "source_sha": current_build()["source_sha"],
                },
                handle,
            )
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        temporary.replace(cache)
    except OSError:
        pass
