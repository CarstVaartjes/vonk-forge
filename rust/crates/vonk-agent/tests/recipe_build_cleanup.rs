use std::{
    cell::{Cell, RefCell},
    collections::BTreeSet,
    time::{Duration, Instant},
};
use uuid::Uuid;
use vonk_agent::{
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    recipe_builder::cleanup_build,
};
use vonk_agent_protocol::RecipeBuildCleanupRequest;

struct Units {
    active: RefCell<BTreeSet<String>>,
    lost_stop_reply: Cell<bool>,
    broken_observation: Cell<u32>,
}
impl ProcessRunner for Units {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        _: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        assert_eq!(program, Program::Systemctl);
        let unit = arguments
            .iter()
            .find(|value| value.ends_with(".service"))
            .unwrap();
        if arguments.iter().any(|value| value == "stop") {
            self.active.borrow_mut().remove(unit);
            if self.lost_stop_reply.replace(false) {
                return Err(ProcessError::Timeout);
            }
        } else if self.broken_observation.get() > 0 {
            self.broken_observation
                .set(self.broken_observation.get() - 1);
            return Ok(ProcessOutput {
                success: true,
                stdout: b"unreadable peer observation".to_vec(),
                stderr: vec![],
            });
        }
        let stdout = if arguments.iter().any(|value| value == "list-units")
            && self.active.borrow().contains(unit)
        {
            format!("{unit} loaded active running fixture\n")
        } else if arguments.iter().any(|value| value == "show") {
            "LoadState=loaded\nActiveState=active\nMainPID=42\nControlGroup=/fixture\n".into()
        } else {
            String::new()
        };
        Ok(ProcessOutput {
            success: true,
            stdout: stdout.into_bytes(),
            stderr: vec![],
        })
    }
}
#[test]
fn process_death_leaves_adapter_and_egress_but_cleanup_reconciles_all_exact_units() {
    let build = Uuid::new_v4();
    let foreign = format!("vonk-runtime-adapter-{}.service", Uuid::new_v4());
    let runner = Units {
        active: RefCell::new(BTreeSet::from([
            format!("vonk-runtime-adapter-{build}.service"),
            format!("vonk-recipe-build-{build}-e.service"),
            foreign.clone(),
        ])),
        lost_stop_reply: Cell::new(true),
        broken_observation: Cell::new(1),
    };
    let started = Instant::now();
    cleanup_build(
        &runner,
        &RecipeBuildCleanupRequest {
            build_id: build,
            operation_id: Uuid::new_v4(),
        },
    )
    .unwrap();
    assert!(started.elapsed() < Duration::from_secs(2));
    assert_eq!(*runner.active.borrow(), BTreeSet::from([foreign]));
    cleanup_build(
        &runner,
        &RecipeBuildCleanupRequest {
            build_id: Uuid::new_v4(),
            operation_id: Uuid::new_v4(),
        },
    )
    .unwrap();
}

struct StorageCleanup(Units);
impl ProcessRunner for StorageCleanup {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        if program == Program::SystemdRun {
            return Ok(ProcessOutput {
                success: true,
                stdout: vec![],
                stderr: vec![],
            });
        }
        self.0.run(program, arguments, timeout)
    }
}
#[test]
fn storage_cleanup_is_exact_and_retains_completed_exports_for_upload_recovery() {
    let data = tempfile::tempdir().unwrap();
    let runtime = tempfile::tempdir().unwrap();
    let build = Uuid::new_v4();
    let selected = data
        .path()
        .join("build-staging")
        .join(format!("{}-{build}", "a".repeat(64)));
    let neighbor =
        data.path()
            .join("build-staging")
            .join(format!("{}-{}", "b".repeat(64), Uuid::new_v4()));
    std::fs::create_dir_all(selected.join("podman-storage")).unwrap();
    std::fs::create_dir_all(&neighbor).unwrap();
    std::fs::write(neighbor.join("retained"), b"neighbor").unwrap();
    let export = data.path().join("builds").join(build.to_string());
    std::fs::create_dir_all(&export).unwrap();
    std::fs::write(export.join("image.docker.tar"), b"completed export").unwrap();
    let runner = StorageCleanup(Units {
        active: RefCell::new(BTreeSet::new()),
        lost_stop_reply: Cell::new(false),
        broken_observation: Cell::new(0),
    });
    let request = RecipeBuildCleanupRequest {
        build_id: build,
        operation_id: Uuid::new_v4(),
    };
    vonk_agent::recipe_builder::cleanup_build_storage(
        &runner,
        data.path(),
        runtime.path(),
        &request,
    )
    .unwrap();
    assert!(!selected.exists());
    assert_eq!(
        std::fs::read(neighbor.join("retained")).unwrap(),
        b"neighbor"
    );
    assert_eq!(
        std::fs::read(export.join("image.docker.tar")).unwrap(),
        b"completed export"
    );
    vonk_agent::recipe_builder::cleanup_build_storage(
        &runner,
        data.path(),
        runtime.path(),
        &request,
    )
    .unwrap();
}
