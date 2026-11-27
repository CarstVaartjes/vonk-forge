"""Expiry of the certificates the controller serves and issues from.

Read from the files actually in use at each scrape: the leaf Caddy serves
(runtime-init renews it in the controller-tls volume) and the intermediate and
root the API stages. A file that is absent has no series; a damaged one reads
as expired.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from cryptography import x509

from vonk_control.settings import SECRETS_ROOT

METRIC = "vonk_controller_certificate_expiry_timestamp_seconds"
CERTIFICATE_FILES: Mapping[str, Path] = {
    "server": Path("/run/vonk-controller-tls/controller-server-certificate"),
    "intermediate": SECRETS_ROOT / "agent-intermediate-certificate",
    "root": SECRETS_ROOT / "controller-ca",
}


def certificate_expiry_lines(files: Mapping[str, Path]) -> list[str]:
    lines = [
        f"# HELP {METRIC} When the controller certificate expires (Unix seconds).",
        f"# TYPE {METRIC} gauge",
    ]
    for name, path in files.items():
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            # Not mounted (an older Compose file) or not staged yet.
            continue
        except OSError:
            content = b""
        lines.append(f'{METRIC}{{certificate="{name}"}} {_expiry(content)}')
    return lines


def _expiry(content: bytes) -> int:
    """The expiry; an unreadable certificate reads as expired, so its alerts fire."""
    try:
        certificate = x509.load_pem_x509_certificate(content)
    except ValueError:
        return 0
    return int(certificate.not_valid_after_utc.timestamp())
