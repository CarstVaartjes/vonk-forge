"""Produce the canonical exact-binary rollback lookup from verified package bytes."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent_protocol/src"))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from vonk_agent_protocol.package_source import AgentPackageSource
from vonk_agent_protocol.package_upgrade import PackageRollbackSource


def package_source(path: Path, *, version: str) -> AgentPackageSource:
    raw = path.read_bytes()
    signature = Path(f"{path}.host.sig").read_text().strip()
    provenance = json.loads(path.with_suffix(".provenance.json").read_bytes())
    subjects = {item["name"]: item["digest"]["sha256"] for item in provenance["subject"]}
    with tempfile.TemporaryDirectory(prefix="package-source-") as temporary:
        subprocess.run(["/usr/bin/dpkg-deb", "--extract", str(path), temporary], check=True, capture_output=True, timeout=60)
        extracted = Path(temporary)
        binary = hashlib.sha256((extracted / "usr/lib/vonk-forge/vonk-agent").read_bytes()).hexdigest()
        helper = hashlib.sha256((extracted / "usr/lib/vonk-forge/vonk-agent-helper").read_bytes()).hexdigest()
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex((extracted / "usr/share/keyrings/vonk-forge-release.pub").read_text().strip()))
        key.verify(bytes.fromhex(signature), b"VONK-HOST-ARTIFACT-V1\x00deb\x00" + hashlib.sha256(raw).digest())
    if subjects.get("vonk-agent") != binary or subjects.get("vonk-agent-helper") != helper:
        raise ValueError("source package provenance does not match package binaries")
    digest = hashlib.sha256(raw).hexdigest()
    return AgentPackageSource(schema_version=2, architecture="linux-arm64",
        build_digest=provenance["predicate"]["buildDefinition"]["externalParameters"]["build_digest"],
        package=PackageRollbackSource(package_sha256=digest, package_signature=signature,
            package_version=version, binary_sha256=binary, helper_sha256=helper),
        package_bytes=len(raw), package_url=f"https://install.vonkforge.ai/artifacts/agent-packages/{digest}/vonk-forge-agent.deb")
