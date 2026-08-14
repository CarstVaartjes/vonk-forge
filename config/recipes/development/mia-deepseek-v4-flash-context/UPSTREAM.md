# Upstream provenance

This source context is derived from the official
[`MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark`](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark)
recipe at commit `f752cd04ab30f2cf42077dd8811a5e1e682d63e7` (2026-08-14).

The files under `patches/` and `LICENSE.mia-upstream` are copied from that
commit. Source bytes are unchanged except that the issue-27, issue-43, and
issue-55 Python files gain the repository's conventional final newline.
`encoding/encoding_dsv4.py` is
copied byte-for-byte from the
official `deepseek-ai/DeepSeek-V4-Flash-0731` model snapshot at revision
`9e165c30e2704aec5d9d593cce3eebd58bbef1cb`.

The upstream GPU-resident thinking-budget and tool-call truncation hotfixes are
applied after the encoding patch. Issue-43 bounded decode service follows issue
27, and suppress-stops-in-reasoning remains enabled by default, matching the
selected recipe. Optional issue-43 diagnostics remain disabled by default.

Vonk Forge supplies only the immutable image-build wrapper, runtime-contract
launcher, and automatic RoCEv2 interface/HCA/GID discovery. Runtime downloads,
SSH orchestration, mutable package installation, optional abliterated weights,
and the optional vision sidecar are deliberately excluded.
