#!/usr/bin/env python3
"""Resolve the unique RoCEv2 interface/HCA/GID for a selected local IPv4."""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path


def resolve_roce(root: Path, raw_address: str) -> tuple[str, str, str] | None:
    address = ipaddress.IPv4Address(raw_address)
    expected_gid = ipaddress.IPv6Address(f"::ffff:{address}")
    matches: list[tuple[str, str, str]] = []
    for hca in sorted(root.glob("*")):
        port = hca / "ports/1"
        for ndev in sorted((port / "gid_attrs/ndevs").glob("*")):
            index = ndev.name
            try:
                interface = ndev.read_text(encoding="utf-8").strip()
                if not interface:
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
                matches.append((interface, hca.name, index))
    return matches[0] if len(matches) == 1 else None


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    raw_address = sys.argv[1]
    try:
        match = resolve_roce(Path("/sys/class/infiniband"), raw_address)
    except ValueError:
        return 2
    if match is None:
        print(
            f"expected one RoCEv2 interface/HCA/GID for {raw_address}",
            file=sys.stderr,
        )
        return 1
    print(*match)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
