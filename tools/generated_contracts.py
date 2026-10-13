"""Prepare contract consumers before pytest imports test modules.

The generated clients and wire schema are ignored build inputs. Generating
them costs tens of seconds, so a stamp records the fingerprint of the
generator inputs and of the outputs they produced; preparation is skipped
while both still match. Any changed input, and any missing or edited output,
regenerates everything. The stamp lives in ``.state`` beside the other
preparation locks and is never a reviewable source.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAMP = Path(".state/generated-contracts.json")
STAMP_SCHEMA_VERSION = 1

#: Everything the two generators read. Directories are walked; generated
#: outputs, caches and installed modules inside them are not inputs.
INPUTS: tuple[str, ...] = (
    "agent_protocol/src",
    "control/src",
    "src/cluster_profiles",
    "schemas",
    "tools/openapi-client",
    "tools/hatch_build.py",
    "install/installer-release-public.pem",
    "control/openapi-python-client.yaml",
    "control/pyproject.toml",
    "control/uv.lock",
    "pyproject.toml",
    "scripts/check_environment.py",
    "scripts/export-agent-wire-schema",
    "scripts/generate-control-clients",
)
_SKIPPED_DIRECTORIES = frozenset({"__pycache__", "node_modules", ".venv"})

#: Outputs of scripts/generate-control-clients, compared byte for byte by
#: scripts/check-generated-determinism.
CLIENT_OUTPUTS: tuple[str, ...] = (
    "control/openapi.json",
    "src/cluster_profiles/schemas/control-openapi.json",
    "src/cluster_profiles/schemas/cli-update-contract.schema.json",
    "src/cluster_profiles/generated_control",
    "control/web/src/api/generated.d.ts",
    "control/web/src/api/runtime.generated.d.ts",
    "control/web/src/api/runtime.generated.js",
    "control/web/src/api/vocabulary.generated.ts",
)
#: Outputs of scripts/generate-agent-wire: the exported schema and the Rust
#: it renders. Cargo renders its own copy into OUT_DIR.
WIRE_OUTPUTS: tuple[str, ...] = (
    "rust/crates/vonk-agent-protocol/schema/wire.json",
    "rust/crates/vonk-agent-protocol/src/generated.rs",
)
#: What preparation itself produces.
PREPARED_OUTPUTS: tuple[str, ...] = (*CLIENT_OUTPUTS, WIRE_OUTPUTS[0])

COMMANDS: tuple[tuple[str, ...], ...] = (
    ("scripts/generate-control-clients",),
    ("scripts/export-agent-wire-schema", "--output", WIRE_OUTPUTS[0]),
)


def _files(root: Path, names: Iterable[str], excluded: frozenset[Path]) -> list[Path]:
    files: list[Path] = []
    for name in names:
        path = root / name
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if not child.is_file():
                    continue
                if _SKIPPED_DIRECTORIES.intersection(
                    child.relative_to(path).parts[:-1]
                ):
                    continue
                if child in excluded or any(
                    parent in excluded for parent in child.parents
                ):
                    continue
                files.append(child)
        elif path.is_file():
            files.append(path)
    return files


def _fingerprint(files: Iterable[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def fingerprints(root: Path = ROOT) -> tuple[str, str | None]:
    """The input fingerprint and, when every output exists, the output one."""
    outputs = [root / name for name in PREPARED_OUTPUTS]
    inputs = _fingerprint(_files(root, INPUTS, frozenset(outputs)), root)
    if not all(path.exists() for path in outputs):
        return inputs, None
    return inputs, _fingerprint(_files(root, PREPARED_OUTPUTS, frozenset()), root)


def is_current(root: Path = ROOT) -> bool:
    """Whether the stamped outputs still match the current inputs."""
    try:
        recorded = json.loads((root / STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if (
        not isinstance(recorded, dict)
        or recorded.get("schema_version") != STAMP_SCHEMA_VERSION
    ):
        return False
    inputs, outputs = fingerprints(root)
    return (
        outputs is not None
        and recorded.get("inputs") == inputs
        and recorded.get("outputs") == outputs
    )


def prepare_generated_contracts(root: Path = ROOT) -> bool:
    """Generate outdated contract consumers once; return whether anything ran.

    xdist workers never generate: the controller prepared the tree before
    starting them, and a worker rewriting modules another worker imports would
    race. ``VONK_TEST_CONTRACT_ROOT`` names a tree this process prepared.
    """
    if (
        os.environ.get("VONK_TEST_CONTRACT_ROOT") == str(root)
        or os.environ.get("PYTEST_XDIST_WORKER") is not None
    ):
        return False
    stamp = root / STAMP
    stamp.parent.mkdir(parents=True, exist_ok=True)
    generated = False
    # Keep the inode: an unlinked lock file would allow independent owners.
    with stamp.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not is_current(root):
            for command in COMMANDS:
                subprocess.run(
                    [str(root / command[0]), *command[1:]],
                    cwd=root,
                    check=True,
                    timeout=1200,
                )
            inputs, outputs = fingerprints(root)
            if outputs is None:
                missing = [
                    name for name in PREPARED_OUTPUTS if not (root / name).exists()
                ]
                raise RuntimeError(
                    f"contract generation left outputs missing: {missing}"
                )
            stamp.write_text(
                json.dumps(
                    {
                        "schema_version": STAMP_SCHEMA_VERSION,
                        "inputs": inputs,
                        "outputs": outputs,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            generated = True
    os.environ["VONK_TEST_CONTRACT_ROOT"] = str(root)
    return generated
