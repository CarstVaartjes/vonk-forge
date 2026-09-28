"""Fetch every catalog and authority source commit the recipe library names."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

library = Path(sys.argv[1]).resolve()
retry = Path(os.environ["GITHUB_WORKSPACE"]) / "scripts/retry-dependency-fetch"
catalog = json.loads((library / "catalog-index.json").read_text(encoding="utf-8"))
sources = [catalog["source_commit"]]
for authority_path in sorted((library / "qualification/authorities").glob("*.json")):
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    sources.append(authority["catalog"]["source_commit"])
if any(
    not isinstance(source, str) or re.fullmatch(r"[0-9a-f]{40}", source) is None
    for source in sources
):
    raise SystemExit(
        "catalog or authority validation source is not an exact Git commit"
    )
for source in sorted(set(sources)):
    subprocess.run(
        [str(retry), "git", "fetch", "--no-tags", "--depth=1", "origin", source],
        cwd=library,
        check=True,
        timeout=600,
    )
    subprocess.run(
        ["git", "cat-file", "-e", f"{source}^{{commit}}"],
        cwd=library,
        check=True,
        timeout=30,
    )
