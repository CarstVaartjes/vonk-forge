#!/usr/bin/env python3
"""Apply the exact Mia patch sequence to the installed vLLM tree at build time."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path("/usr/local/lib/python3.12/dist-packages/vllm")
SOURCE = Path("/opt/vonk-build")
PATCHES = SOURCE / "patches"
CANONICAL_NO_NEWLINE = {
    "hotfix-dsv4-issue27-partial-prefill-concurrency.py",
    "hotfix-dsv4-issue43-decode-fairness-and-diag.py",
    "hotfix-dsv4-issue55-tool-truncation.py",
}
EXPECTED = {
    "apply-reasoning-default.py": "f42787340f6c115ead869cdf5076efc45c4e3f54a4c1c04cbcd2de8828aa1947",
    "hotfix-encoding-dsv4-issue21.py": "1a74f6c4ec6a2b7cd2ff01f19b52fbf4ced980a22f08b9d75a6aae1bff0d0548",
    "hotfix-dsv4-issue31-v2-thinking-budget-gpu.py": "7e6ee3e6852dc4003a5d9e7f1c62e316010858722ff3644467e1f4db57d2d909",
    "hotfix-dsv4-issue55-tool-truncation.py": "53f26da9039eb6d99baa6c141c6ed916b292d406da292a5e762012c5ef423dec",
    "hotfix-nvfp4-ds-mla-issue22.sh": "4999ed58c4c2ca0903bc21fcdb6db50d481396ded62066e4132ea609096b13bf",
    "hotfix-dsv4-mtp-buffer-50312.sh": "18dee7b92db1c6c55983c7a9df4d6c27c5a09d9be2225cd54207837fe94ecfe0",
    "hotfix-dsv4-adaptive-topk-50004.sh": "561a6ebd295964e3a37df07c96259a1a2eb0d7e6aaef5ac5ca73ecb0cebf7493",
    "hotfix-dsv4-skip-topk-49486.sh": "636fd162fefc2a156750027b731a9eb136e7993f2552389adf7e3647c5b4dc7b",
    "hotfix-dsv4-dense-prefill-indexer-48407.sh": "c2fa444ea40af9225f3063b3be3a5827f4cada9b0ddf84e156176a23e99a2e6b",
    "hotfix-dsv4-skip-empty-c128-48957.sh": "dabafb64f9273c37659027706920d175d5ed0a6b0cdd53fb5be784f408d7990e",
    "hotfix-dsv4-flashmla-workspace-50298.sh": "a7f557b264d247fbc65bfe49cc6d05e0780e4c6bebcdaf3633ace55338fa4268",
    "hotfix-dsv4-grammar-advance.sh": "99f5e0d3737a8a074c4c85b7348882a91a4d96a12bcf0d65de4d1c751a4d8abd",
    "hotfix-dsv4-issue27-partial-prefill-concurrency.py": "e87e14a6dc45ccbbdea2940d9594f239f6d8dbda7b82d7a094f45bcaa2dfb450",
    "hotfix-dsv4-issue43-decode-fairness-and-diag.py": "f362f6289fabefd17d41007637e99a503f5b282dbb13b21cd203a3c30b844de6",
    "hotfix-dsv4-issue26-hybrid-swa-min.py": "acdf9aa2705de248333b3ba6ddeb20aea67b5582f408552e407c7a670b20ee82",
    "hotfix-dsv4-suppress-stops-in-reasoning.py": "89df901d5d5853e79d71d48e1f2f1a4302ac688b5e2d3788c8551a7fe8477f21",
}


def checked_patch(name: str) -> Path:
    path = PATCHES / name
    payload = path.read_bytes()
    if name in CANONICAL_NO_NEWLINE and payload.endswith(b"\n"):
        payload = payload[:-1]
        path.write_bytes(payload)
    if hashlib.sha256(payload).hexdigest() != EXPECTED[name]:
        raise SystemExit(f"patch hash mismatch: {name}")
    return path


def run(name: str, *arguments: str, environment: dict[str, str] | None = None) -> None:
    path = checked_patch(name)
    command = ["python3" if path.suffix == ".py" else "bash", str(path), *arguments]
    subprocess.run(command, check=True, env=environment)


if not ROOT.is_dir():
    raise SystemExit(f"vLLM tree is missing: {ROOT}")
encoding = SOURCE / "encoding/encoding_dsv4.py"
if hashlib.sha256(encoding.read_bytes()).hexdigest() != (
    "bdbd57c132a1b3725042323d02b98b9d1df28e5f388f134399555d041f5055e0"
):
    raise SystemExit("official encoding hash mismatch")
destination = ROOT / "tokenizers/deepseek_v4_encoding.py"
shutil.copyfile(encoding, destination)

run("apply-reasoning-default.py")
run("hotfix-encoding-dsv4-issue21.py", str(destination))
run("hotfix-dsv4-issue31-v2-thinking-budget-gpu.py", str(ROOT))
run("hotfix-dsv4-issue55-tool-truncation.py", str(ROOT))
run("hotfix-nvfp4-ds-mla-issue22.sh")

shell_environment = {**os.environ, "VLLM_ROOT": str(ROOT)}
for patch in (
    "hotfix-dsv4-mtp-buffer-50312.sh",
    "hotfix-dsv4-adaptive-topk-50004.sh",
    "hotfix-dsv4-skip-topk-49486.sh",
    "hotfix-dsv4-dense-prefill-indexer-48407.sh",
    "hotfix-dsv4-skip-empty-c128-48957.sh",
    "hotfix-dsv4-flashmla-workspace-50298.sh",
    "hotfix-dsv4-grammar-advance.sh",
):
    run(patch, environment=shell_environment)

run("hotfix-dsv4-issue27-partial-prefill-concurrency.py")
run("hotfix-dsv4-issue43-decode-fairness-and-diag.py")
run("hotfix-dsv4-issue26-hybrid-swa-min.py")
run(
    "hotfix-dsv4-suppress-stops-in-reasoning.py",
    str(ROOT / "v1/engine/detokenizer.py"),
)

for backup in ROOT.rglob("*.bak"):
    backup.unlink()
for cache in ROOT.rglob("__pycache__"):
    shutil.rmtree(cache)
