"""Expiry of the certificates the controller serves and issues from.

Read from the files actually in use at each scrape: the leaf Caddy serves
(runtime-init renews it in the controller-tls volume) and the intermediate and
root the API stages. Each certificate names its files in the order Caddy's
entrypoint chooses them: the first present one is read, so the server leaf
falls back to the installer's copy exactly when Caddy does. A certificate with
no file present has no series; a damaged one reads as expired.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from cryptography import x509

from vonk_control.settings import SECRETS_ROOT

METRIC = "vonk_controller_certificate_expiry_timestamp_seconds"
CERTIFICATE_FILES: Mapping[str, tuple[Path, ...]] = {
    "server": (
        Path("/run/vonk-controller-tls/controller-server.pem"),
        SECRETS_ROOT / "controller-server-certificate",
    ),
    "intermediate": (SECRETS_ROOT / "agent-intermediate-certificate",),
    "root": (SECRETS_ROOT / "controller-ca",),
}


def certificate_expiry_lines(files: Mapping[str, tuple[Path, ...]]) -> list[str]:
    lines = [
        f"# HELP {METRIC} When the controller certificate expires (Unix seconds).",
        f"# TYPE {METRIC} gauge",
    ]
    for name, candidates in files.items():
        content = _first_present(candidates)
        if content is not None:
            lines.append(f'{METRIC}{{certificate="{name}"}} {_expiry(content)}')
    return lines


def _first_present(candidates: tuple[Path, ...]) -> bytes | None:
    for path in candidates:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            # Not mounted (an older Compose file) or not staged yet.
            continue
        except OSError:
            return b""
    return None


def _expiry(content: bytes) -> int:
    """The expiry; an unreadable certificate reads as expired, so its alerts fire."""
    try:
        certificate = x509.load_pem_x509_certificate(content)
    except ValueError:
        return 0
    return int(certificate.not_valid_after_utc.timestamp())
