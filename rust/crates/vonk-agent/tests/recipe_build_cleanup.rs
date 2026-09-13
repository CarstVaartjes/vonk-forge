use std::{cell::RefCell, collections::VecDeque, time::Duration};

use uuid::Uuid;
use vonk_agent::{
    process::{ProcessError, ProcessOutput, ProcessRunner, Program, SystemProcessRunner},
    recipe_builder::cleanup_build,
};
use vonk_agent_protocol::RecipeBuildCleanupRequest;

fn request() -> RecipeBuildCleanupRequest {
    serde_json::from_value(serde_json::json!({
        "schema_version": 1,
        "build_id": Uuid::new_v4(),
        "operation_id": Uuid::new_v4(),
    }))
    .unwrap()
}

struct Replies(RefCell<VecDeque<ProcessOutput>>);

impl ProcessRunner for Replies {
    fn run(
        &self,
        program: Program,
        _: &[String],
        _: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        assert_eq!(program, Program::Systemctl);
        Ok(self.0.borrow_mut().pop_front().expect("unexpected command"))
    }
}

fn output(success: bool, stdout: &str) -> ProcessOutput {
    ProcessOutput {
        success,
        stdout: stdout.as_bytes().to_vec(),
        stderr: vec![],
    }
}

#[test]
fn cleanup_does_not_turn_failed_inspection_or_stop_into_free_capacity() {
    let request = request();
    let listed = format!(
        "vonk-recipe-build-{}.service loaded active running fixture\n",
        request.operation_id
    );
    for replies in [
        vec![output(false, "")],
        vec![output(true, &listed), output(false, "")],
        vec![output(true, &listed), output(true, "")],
        vec![
            output(true, &listed),
            output(true, "LoadState=loaded\nActiveState=active\nMainPID=42\n"),
            output(false, ""),
        ],
        vec![
            output(true, &listed),
            output(true, "LoadState=loaded\nActiveState=active\nMainPID=42\n"),
            output(true, ""),
            output(true, &listed),
            output(true, "LoadState=loaded\nActiveState=inactive\nMainPID=42\n"),
        ],
    ] {
        let runner = Replies(RefCell::new(replies.into()));
        assert!(cleanup_build(&runner, &request).is_err());
    }
}

#[test]
#[ignore = "requires the designated unprivileged systemd user-manager fixture"]
fn cleanup_stops_only_its_real_transient_service() {
    let runner = SystemProcessRunner;
    let selected = request();
    let unrelated = request();
    struct Units(Vec<String>);
    impl Drop for Units {
        fn drop(&mut self) {
            for unit in &self.0 {
                let _ = SystemProcessRunner.run(
                    Program::Systemctl,
                    &["--user".into(), "stop".into(), unit.clone()],
                    Duration::from_secs(10),
                );
            }
        }
    }
    let mut units = Units(vec![]);
    for request in [&selected, &unrelated] {
        let unit = format!("vonk-recipe-build-{}.service", request.operation_id);
        units.0.push(unit.clone());
        let result = runner
            .run(
                Program::SystemdRun,
                &[
                    "--user".into(),
                    "--collect".into(),
                    format!("--unit={unit}"),
                    "--property=KillMode=control-group".into(),
                    "/usr/bin/sleep".into(),
                    "600".into(),
                ],
                Duration::from_secs(10),
            )
            .unwrap();
        assert!(
            result.success,
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
    }
    let evidence = cleanup_build(&runner, &selected).unwrap();
    assert!(evidence.stopped);
    let retained = runner
        .run(
            Program::Systemctl,
            &["--user".into(), "is-active".into(), units.0[1].clone()],
            Duration::from_secs(10),
        )
        .unwrap();
    assert!(retained.success, "unrelated build was stopped");
    // Replaying a confirmed cleanup is safe after systemd collects the unit.
    cleanup_build(&runner, &selected).unwrap();
}
