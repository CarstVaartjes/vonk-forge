"""Container-local protocol readiness for the networkless update signer."""

from __future__ import annotations

import os
from pathlib import Path

from .update_signer import check_signer_ready


def main() -> None:
    check_signer_ready(
        Path(os.environ.get("VONK_UPDATE_SIGNER_SOCKET", "/run/vonk-signer/signer.sock"))
    )


if __name__ == "__main__":
    main()
