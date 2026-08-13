# Upstream provenance

This source context is derived from the official
[`MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark`](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark)
recipe at commit `103af68cad84a153c8e6bd3b15e6414a12b71e05` (2026-08-13).

The files under `patches/` and `LICENSE.mia-upstream` are copied from that
commit. Source bytes are unchanged except that the issue-27 and issue-43 Python
files gain the repository's conventional final newline.
`encoding/encoding_dsv4.py` is
copied byte-for-byte from the
official `deepseek-ai/DeepSeek-V4-Flash-0731` model snapshot at revision
`9e165c30e2704aec5d9d593cce3eebd58bbef1cb`.

The upstream issue-43 bounded decode-service hotfix is applied after issue 27,
and suppress-stops-in-reasoning remains enabled by default, matching the
selected recipe. Optional issue-43 diagnostics remain disabled by default.

Vonk Forge supplies only the immutable image-build wrapper, runtime-contract
launcher, and automatic RoCEv2 interface/HCA/GID discovery. Runtime downloads,
SSH orchestration, mutable package installation, optional abliterated weights,
and the optional vision sidecar are deliberately excluded.
