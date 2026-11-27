"""Find one real site's identifiers on an added line.

Examples, fixtures and docs use RFC 5737 TEST-NET addresses (192.0.2.0/24,
198.51.100.0/24), 10.0.0.0/8 where Rust needs a private address, and neutral
Spark names (spark-aaaa, spark-bbbb). The owner's home LAN (192.168.1.<n>) and
the two real Spark hostnames are refused, in every file type.
"""

from __future__ import annotations

import re

KIND = "site-identifier"
_REAL_HOSTS = ("35" + "42", "22" + "97")
PATTERN = re.compile(
    r"\b192\.168\.0*1\.(?:\d{1,3}\b|\{)"
    + r"|\b(?:spark|node)[-_ ](?:"
    + "|".join(_REAL_HOSTS)
    + r")\b"
)


def scan_source(source: str) -> list[int]:
    return [
        number
        for number, line in enumerate(source.splitlines(), start=1)
        if PATTERN.search(line)
    ]
