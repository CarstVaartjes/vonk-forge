"""The per-job secret a workload's ranks authenticate each other with.

Derived, never stored: ``HMAC(HKDF(token signing key), parent job id)``. Every
rank order of one submit shares the parent job, so they get the same value;
every new job gets a new one. It reaches a Spark only in a signed, short-lived
Start grant (``ExecuteContainerRuntimeRequestOperation.peer_secret``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_KEY_INFO = b"vonk-peer-secret-key/v1"
_MESSAGE_DOMAIN = b"vonk-peer-secret/v1\0"


def derive_peer_secret(master_key: bytes, parent_job_id: str) -> str:
    """The 43-character url-safe secret of one job."""

    subkey = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO
    ).derive(master_key)
    mac = hmac.new(
        subkey, _MESSAGE_DOMAIN + parent_job_id.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
