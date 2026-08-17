#!/usr/bin/env python3
"""Verify a canonical hash over every file the Mia patch sequence may change."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

TARGETS = (
    "config/vllm.py",
    "entrypoints/openai/chat_completion/serving.py",
    "model_executor/layers/sparse_attn_indexer.py",
    "models/deepseek_v32/nvidia/attention.py",
    "models/deepseek_v4/attention.py",
    "models/deepseek_v4/common/ops/cache_utils.py",
    "models/deepseek_v4/compressor.py",
    "models/deepseek_v4/nvidia/flashmla.py",
    "models/deepseek_v4/nvidia/model.py",
    "models/deepseek_v4/sparse_mla.py",
    "tokenizers/deepseek_v4.py",
    "tokenizers/deepseek_v4_encoding.py",
    "v1/attention/backends/mla/flashmla_sparse.py",
    "v1/core/kv_cache_coordinator.py",
    "v1/core/sched/scheduler.py",
    "v1/engine/detokenizer.py",
    "v1/engine/input_processor.py",
    "v1/structured_output/__init__.py",
    "v1/worker/gpu/model_runner.py",
    "v1/worker/gpu/sample/sampler.py",
    "v1/worker/gpu/sample/thinking_budget_gpu.py",
)


def digest(root: Path) -> str:
    value = hashlib.sha256()
    for name in TARGETS:
        path = root / name
        relative = name.encode()
        value.update(len(relative).to_bytes(8, "big"))
        value.update(relative)
        if not path.is_file():
            value.update(b"M")
            continue
        payload = path.read_bytes()
        value.update(b"F")
        value.update(len(payload).to_bytes(8, "big"))
        value.update(payload)
    return value.hexdigest()


if len(sys.argv) != 3:
    raise SystemExit("usage: verify-patched-tree.py ROOT EXPECTED_SHA256")
actual = digest(Path(sys.argv[1]))
if actual != sys.argv[2]:
    raise SystemExit(f"patched vLLM tree mismatch: expected {sys.argv[2]}, got {actual}")
print(actual)
