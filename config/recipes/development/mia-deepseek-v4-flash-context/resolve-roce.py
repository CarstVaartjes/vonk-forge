#!/usr/bin/env python3
"""Resolve the unique RoCEv2 HCA/GID for a controller-selected local IPv4."""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    interface, raw_address = sys.argv[1:]
    address = ipaddress.IPv4Address(raw_address)
    expected_gid = ipaddress.IPv6Address(f"::ffff:{address}")
    matches: list[tuple[str, str]] = []
    for hca in sorted(Path("/sys/class/infiniband").glob("*")):
        port = hca / "ports/1"
        for ndev in sorted((port / "gid_attrs/ndevs").glob("*")):
            index = ndev.name
            try:
                if ndev.read_text(encoding="utf-8").strip() != interface:
                    continue
                gid_type = (port / "gid_attrs/types" / index).read_text(
                    encoding="utf-8"
                )
                gid = ipaddress.IPv6Address(
                    (port / "gids" / index).read_text(encoding="utf-8").strip()
                )
            except (OSError, ValueError):
                continue
            if gid_type.strip() == "RoCE v2" and gid == expected_gid:
                matches.append((hca.name, index))
    if len(matches) != 1:
        print(
            f"expected one RoCEv2 HCA/GID for {interface} {address}; "
            f"found {len(matches)}",
            file=sys.stderr,
        )
        return 1
    print(*matches[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

