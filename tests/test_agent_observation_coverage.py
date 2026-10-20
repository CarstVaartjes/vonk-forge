"""Whole-tree observation dispatch gate, paired with native per-kind behavior.

A new wire kind, a missing handler or a fallback returning unknown cannot pass
by leaving the shared startup/tick dispatcher incomplete.
"""

import re
from pathlib import Path

from vonk_agent_protocol.contracts import AgentOperation

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "rust/crates/vonk-agent/src/executor/operation_observation.rs"


def observation_gaps(source: str) -> set[str]:
    mapping, handlers = source.split("enum ProbeError", 1)
    bindings = dict(re.findall(r"AgentOperation::(\w+) => Self::(\w+),", mapping))
    arms = list(
        re.finditer(
            r"(ObservationKind::\w+(?:\s*\|\s*ObservationKind::\w+)*)\s*=>", handlers
        )
    )
    probes = {}
    for index, arm in enumerate(arms):
        body = handlers[
            arm.end() : arms[index + 1].start()
            if index + 1 < len(arms)
            else len(handlers)
        ]
        for handler in re.findall(r"ObservationKind::(\w+)", arm[1]):
            probes[handler] = body
    missing = set()
    for kind in AgentOperation:
        variant = "".join(part.title() for part in re.split(r"[._]", kind.value))
        handler = bindings.get(variant)
        body = probes.get(handler or "", "")
        # The handler must reach an owner probe. Native tests additionally
        # exercise the effect and custody rather than just these call sites.
        if not re.search(
            r"self\.(?:runtime|client|observe_)|crate::|super::upgrade_observation::observe_upgrade",
            body,
        ):
            missing.add(kind.value)
    if re.search(r"\b_\s*=>", mapping):
        missing.add("wildcard")
    return missing


def test_every_wire_kind_has_a_real_startup_and_tick_observer() -> None:
    assert not observation_gaps(SOURCE.read_text())


def test_observation_gate_rejects_new_kind_missing_and_placeholder_handlers() -> None:
    source = SOURCE.read_text()
    assert observation_gaps(
        source.replace("AgentOperation::AgentUpgradeV1 => Self::Upgrade,", "")
    )
    assert observation_gaps(
        re.sub(
            r"ObservationKind::Upgrade => \{.*?\n            \}",
            "ObservationKind::Upgrade => Ok(unknown(String::new())),",
            source,
            count=1,
            flags=re.DOTALL,
        )
    )
