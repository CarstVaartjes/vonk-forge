#![forbid(unsafe_code)]

use std::{fs, path::Path};

/// An optional dependency never blocks the mandatory capacity observation:
/// only these programs may abort the inventory with `startup_probe(..)?`.
/// Podman (recipe builds only) is deliberately absent; a dead Podman degrades
/// the build capabilities and the report is still produced.
const CAPACITY_CRITICAL_PROBES: [&str; 2] = [
    // container_runtime_version; loads and installs run on Docker.
    "Program::Docker",
    // The CDI device list; GPU workloads cannot start without it.
    "Program::NvidiaCtk",
];

#[test]
fn only_capacity_critical_probes_may_abort_the_inventory() {
    let source =
        fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/inventory.rs")).unwrap();
    let mut aborting = Vec::new();
    for (start, _) in source.match_indices("startup_probe(") {
        if source[..start].ends_with("fn ") {
            continue;
        }
        let mut depth = 0_usize;
        let mut end = start;
        for (offset, character) in source[start..].char_indices() {
            match character {
                '(' => depth += 1,
                ')' => {
                    depth -= 1;
                    if depth == 0 {
                        end = start + offset + 1;
                        break;
                    }
                }
                _ => {}
            }
        }
        if source[end..].starts_with('?') {
            aborting.push(&source[start..end]);
        }
    }
    for call in aborting {
        assert!(
            CAPACITY_CRITICAL_PROBES
                .iter()
                .any(|program| call.contains(program)),
            "startup_probe(..)? aborts the whole inventory; degrade the capability instead: {call}"
        );
    }
}
