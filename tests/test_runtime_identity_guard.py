"""Catch new numeric container builders that omit the platform NSS identity.

This syntax guard inventories run builders; behavioural tests cover the shared
helper's service/job launch and the scratch build-probe producer.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def missing_identity(source: str) -> bool:
    source = source.split("#[cfg(test)]", 1)[0]
    return (
        '"run"' in source
        and re.search(r'"--user[="]', source) is not None
        and "runtime_identity::" not in source
    )


def test_numeric_runtime_builders_supply_identity() -> None:
    # This is the signed projection, consumed by the privileged helper; it
    # cannot choose host NSS paths. Launch policy adds the private mounts.
    projection = ROOT / "rust/crates/vonk-agent-protocol/src/compiled_oci/invocation.rs"
    helper = ROOT / "rust/crates/vonk-agent-helper/src/operations/start.rs"
    assert (
        "self.runtime_identity_mounts(validated.uid, validated.gid)?"
        in helper.read_text()
    )
    missing = []
    for path in (ROOT / "rust/crates").rglob("*.rs"):
        if (
            path == projection
            or path.name == "tests.rs"
            or any(part in {"tests", "test_support"} for part in path.parts)
        ):
            continue
        if missing_identity(path.read_text()):
            missing.append(str(path.relative_to(ROOT)))
    assert not missing, missing


def test_identity_guard_rejects_environment_only_workaround() -> None:
    source = 'vec!["run", "--user=10001:10001", "--env", "USER=vonk-runtime"]'
    assert missing_identity(source)
    assert not missing_identity(
        source + "; runtime_identity::append_to(&mut archive, uid, gid)?;"
    )
    assert not missing_identity('vec!["--user", "is-active", "unit"]')
