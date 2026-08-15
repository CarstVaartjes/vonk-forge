#!/usr/bin/env python3
"""Native exec wrapper binding placement rendezvous to Anemll vLLM flags."""

from __future__ import annotations

import os
import sys

arguments = sys.argv[1:]
if "--nnodes" in arguments:
    master_address = os.environ.get("VONK_MASTER_ADDR")
    master_port = os.environ.get("VONK_MASTER_PORT")
    if not master_address or not master_port:
        raise SystemExit("distributed vLLM requires placement rendezvous")
    arguments.extend(("--master-addr", master_address, "--master-port", master_port))
os.execv("/usr/local/bin/vllm", ("/usr/local/bin/vllm", *arguments))
