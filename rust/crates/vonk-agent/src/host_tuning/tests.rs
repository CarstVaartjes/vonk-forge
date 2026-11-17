#![cfg(test)]

use std::{fs, time::Duration};

use super::*;
use crate::process::{ProcessError, ProcessOutput};

const VECTOR: &str = include_str!(
    "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/host-tuning-v1.json"
);

struct Systemd(Option<String>);

impl ProcessRunner for Systemd {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        _timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        assert_eq!(program, Program::Systemctl);
        assert_eq!(arguments.len(), 3 + UNITS.len());
        let stdout = self.0.clone().ok_or(ProcessError::Timeout)?;
        Ok(ProcessOutput {
            exit_code: Some(0),
            success: true,
            stdout: stdout.into_bytes(),
            stderr: vec![],
        })
    }
}

#[test]
fn the_shared_host_reads_as_the_shared_observation() {
    let vector: serde_json::Value = serde_json::from_str(VECTOR).unwrap();
    let host = &vector["host"];
    let root = tempfile::tempdir().unwrap();
    for (path, text) in host["files"].as_object().unwrap() {
        let path = root.path().join(path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text.as_str().unwrap()).unwrap();
    }
    for name in host["snap_directories"].as_array().unwrap() {
        fs::create_dir_all(root.path().join("snap").join(name.as_str().unwrap())).unwrap();
    }
    // A file in /snap (its README) is not a snap.
    fs::write(root.path().join("snap/README"), b"").unwrap();
    let runner = Systemd(Some(host["systemctl_show"].as_str().unwrap().to_owned()));
    let observed = observe(&runner, root.path(), host["page_size"].as_u64().unwrap());
    // The expectation goes through the generated contract; canonical bytes
    // make an omitted field and an explicit null agree too.
    let expected: HostTuning = serde_json::from_value(vector["observation"].clone()).unwrap();
    assert_eq!(
        vonk_agent_protocol::canonical_generated_json(&observed).unwrap(),
        vonk_agent_protocol::canonical_generated_json(&expected).unwrap()
    );
}

#[test]
fn each_reading_that_fails_is_unknown_on_its_own() {
    // Fault: systemd does not answer and the host has no /proc or /snap here.
    let root = tempfile::tempdir().unwrap();
    let observed = observe(&Systemd(None), root.path(), 4096);
    assert_eq!(observed.units, None);
    assert_eq!(observed.snaps, None);
    assert_eq!(observed.watermark_boost_factor, None);
    assert_eq!(observed.kernel_splash, None);
    assert_eq!(observed.free_memory, None);
    // An answer that names none of the units is no answer either.
    assert_eq!(parse_show("active\n"), None);
    // systemd's unset u64::MAX is unknown memory, not a value the contract refuses.
    let unset = parse_show(
        "MemoryCurrent=18446744073709551615\nId=bluetooth.service\nLoadState=loaded\nActiveState=active\n",
    )
    .unwrap();
    assert_eq!(unset[0].memory_bytes, None);
    // A unit that is not installed is never named, even when systemd answers.
    let missing = "Id=cups.path\nLoadState=not-found\nActiveState=inactive\n";
    assert_eq!(parse_show(missing), Some(vec![]));
}

#[test]
fn every_contract_unit_is_asked_for_in_order() {
    for (index, unit) in UNITS.iter().enumerate() {
        assert_eq!(position(*unit), index);
    }
}
