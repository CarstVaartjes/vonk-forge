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
EXPECTED = {
    "apply-reasoning-default.py": "505d3345ba2a5369481896e83f94003d3e8182253f783a9852c6f45413ebaed0",
    "hotfix-encoding-dsv4-issue21.py": "c75d160245cb563d6e9a6adaee9bf7a4cd55ed5268b5ca89856977d293df9816",
    "hotfix-dsv4-issue55-tool-truncation.py": "0dbb8a18d41325d518c221b3cfbd148c3a092e37c1816ba565b62e28172dd773",
    "hotfix-nvfp4-ds-mla-issue22.sh": "52de6d0cd06f571cfdbbb856bfea4a098a5118ce769de739a18b51044300772a",
    "hotfix-gb10-spin-wait.sh": "b7deed123348d78c8e7ae3f99d9107b59a798d0b3c7840b7cccaadfd8418de71",
    "hotfix-dsv4-mtp-buffer-50312.sh": "8ad604b767e09390a958cd6ffd907dd9260bb92d680b8d4d5e702ff61fb787f4",
    "hotfix-dsv4-skip-topk-49486.sh": "431eff0d51c107afacc8ddb76e34c5a57d146341bf5a0d982569e8f89fc474ed",
    "hotfix-dsv4-dense-prefill-indexer-48407.sh": "6d731f1b03b6c17275c8f0af82ee5dfa3ff9d778d25468b4edc96fbd356ffa23",
    "hotfix-dsv4-skip-empty-c128-48957.sh": "bcae8526f474f885f0af681aaa596e613fa94f8bf95847f1e71c5ea4970ccd27",
    "hotfix-dsv4-flashmla-workspace-50298.sh": "213fd93fb6c4dd70f38eefbd331f0ce08b64331feb2ff03643394857acd96078",
    "hotfix-dsv4-grammar-advance.sh": "6318c0959816156ba0015fba9d3d56e4e128acdfb778aee373d9bf227c6faaa5",
    "hotfix-vllm-empty-encoder-output.py": "e417bcdcb6d62f4790885fe5c64bef3a3015a17cea00e3901eb3e2f4b7cf35a6",
    "hotfix-dsv4-issue27-partial-prefill-concurrency.py": "31e7b14213dc6983c07716cf625c4245a42f9d884733e5f7e21a79ab459a8f8b",
    "hotfix-dsv4-issue43-decode-fairness-and-diag.py": "0059144ce08e825354718c8b0aa3799dcf434045f40241f75a4211fe4f199dc4",
    "hotfix-dsv4-issue26-hybrid-swa-min.py": "8c76a65207d5f30b898cf5f60e39b8a59e4febb3217c34fe57f6a7fb225a3c3f",
    "hotfix-dsv4-suppress-stops-in-reasoning.py": "618a66c58fc422ae65d0f08018fac69370657e4ead1285c8104a56f507f6279f",
}


def checked_patch(name: str) -> Path:
    path = PATCHES / name
    payload = path.read_bytes()
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
    "abc0d26120250dda0ae077dc64aa28836026e61e970854aaeb792445e6a0dde6"
):
    raise SystemExit("official encoding hash mismatch")
destination = ROOT / "tokenizers/deepseek_v4_encoding.py"
shutil.copyfile(encoding, destination)

run("apply-reasoning-default.py")
run("hotfix-encoding-dsv4-issue21.py", str(destination))
run("hotfix-dsv4-issue55-tool-truncation.py", str(ROOT))

shell_environment = {**os.environ, "VLLM_ROOT": str(ROOT)}
run("hotfix-nvfp4-ds-mla-issue22.sh", environment=shell_environment)
run("hotfix-gb10-spin-wait.sh", environment=shell_environment)
for patch in (
    "hotfix-dsv4-mtp-buffer-50312.sh",
    "hotfix-dsv4-skip-topk-49486.sh",
    "hotfix-dsv4-dense-prefill-indexer-48407.sh",
    "hotfix-dsv4-skip-empty-c128-48957.sh",
    "hotfix-dsv4-flashmla-workspace-50298.sh",
    "hotfix-dsv4-grammar-advance.sh",
):
    run(patch, environment=shell_environment)

run("hotfix-vllm-empty-encoder-output.py")
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
