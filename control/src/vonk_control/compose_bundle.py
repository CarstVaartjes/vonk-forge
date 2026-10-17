"""Whether the NAS Compose file is the one this Controller release was built with.

Pulling a release refreshes images only; the Compose file stays as the NAS
installer last wrote it. The Compose renderer stamps the digest of its template
into the Controller environment, and the image build embeds the digest of the
template it was built from. A difference is reported as attention with the fix
(rerun the NAS installer), never as a refusal to run.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ENVIRONMENT_VARIABLE = "VONK_COMPOSE_BUNDLE_DIGEST"
EMBEDDED_DIGEST = Path("/usr/local/share/vonk-forge/compose-bundle-digest")
_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ComposeBundle:
    expected: str
    # The stamped digest, or "missing" for a Compose file from before the
    # stamp existed, or "invalid".
    running: str

    @property
    def current(self) -> bool:
        return self.running == self.expected


def compose_bundle(
    environ: Mapping[str, str] = os.environ, embedded: Path = EMBEDDED_DIGEST
) -> ComposeBundle | None:
    """The comparison, or ``None`` outside a release image (nothing to compare)."""

    try:
        expected = embedded.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    if _DIGEST.fullmatch(expected) is None:
        return None
    stamped = environ.get(ENVIRONMENT_VARIABLE)
    if not stamped:
        running = "missing"
    elif _DIGEST.fullmatch(stamped) is None:
        running = "invalid"
    else:
        running = stamped
    return ComposeBundle(expected=expected, running=running)
