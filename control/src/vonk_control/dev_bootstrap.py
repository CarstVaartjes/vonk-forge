"""Run development runtime projection and remain healthy for NAS project UIs."""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import dev_init


def main() -> None:
    result = dev_init.main()
    if result != 0:
        raise SystemExit(result)
    supervisor = Path(os.environ["VONK_DEV_SUPERVISOR_ROOT"])
    supervisor.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(supervisor, 10002, 10001)
    Path("/tmp/bootstrap-ready").touch()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
