"""Pattern-bound identifier strings the agent protocol and Controller share.

Each constrained id type is defined here once; contracts import it.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

DIGEST_PATTERN = r"^[0-9a-f]{64}$"
OCI_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
# Any canonical-form UUID, whatever its version or variant nibbles.
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
UUID4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"

Digest = Annotated[str, StringConstraints(pattern=DIGEST_PATTERN)]
OciDigest = Annotated[str, StringConstraints(pattern=OCI_DIGEST_PATTERN)]
NodeId = Annotated[str, StringConstraints(pattern=NODE_PATTERN)]
UuidId = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]
Uuid4Text = Annotated[str, StringConstraints(pattern=UUID4_PATTERN)]
