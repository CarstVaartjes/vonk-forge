#!/usr/bin/env python3
"""Apply the reasoning-effort normalization used by the pinned Mia recipe."""

from pathlib import Path


TARGET = Path(
    "/usr/local/lib/python3.12/dist-packages/vllm/tokenizers/deepseek_v4.py"
)
OLD = '''elif reasoning_effort in ("max", "xhigh"):
                reasoning_effort = "max"
            else:
                reasoning_effort = "high"'''
NEW = '''elif reasoning_effort in ("max", "xhigh"):
                reasoning_effort = "max"
            elif reasoning_effort == "high":
                reasoning_effort = "high"
            else:
                reasoning_effort = "low"'''


source = TARGET.read_text(encoding="utf-8")
if NEW not in source:
    if OLD not in source:
        raise SystemExit("reasoning-effort normalization anchor is missing")
    TARGET.write_text(source.replace(OLD, NEW, 1), encoding="utf-8")
