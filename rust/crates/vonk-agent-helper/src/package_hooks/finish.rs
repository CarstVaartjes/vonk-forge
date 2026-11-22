//! The transient finisher unit: after dpkg exits, restart the helper (so the
//! new binary serves) and then the agent, with no agent talking to a helper
//! of another release in between.
use vonk_agent_protocol::generated::OperatorActionName;

use super::{Host, best_effort};

const SYSTEMCTL: &str = "/usr/bin/systemctl";
const HELPER_UNIT: &str = "vonk-forge-package-helper.service";
const AGENT_UNITS: [&str; 2] = ["vonk-forge-agent.service", "vonk-forge-monitor.service"];
/// Polls of 100 ms: 120 s for dpkg to leave, inside the unit's `RuntimeMaxSec`.
const DPKG_EXIT_POLLS: u32 = 1200;
const STAGES: usize = 4;

/// One step of the restart. Each transition is one command or proof; the
/// agent is stopped for every stage from `RestartHelper` to `StartAgent`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Stage {
    StopAgent,
    RestartHelper,
    ProveHelper { helper_pid: u32 },
    StartAgent,
}

pub(super) fn run(host: &mut impl Host, dpkg_pid: u32, dpkg_start: u64) -> Result<(), String> {
    // Nothing is stopped yet, so waiting too long needs no repair.
    wait_for_dpkg(host, dpkg_pid, dpkg_start)?;
    let reference = reference_digest(host);
    let mut stage = Stage::StopAgent;
    // Every transition moves forward, so the machine settles within its stages.
    for _ in 0..STAGES {
        match advance(host, stage, reference.as_deref()) {
            Ok(Some(next)) => stage = next,
            Ok(None) => return Ok(()),
            Err(error) => return settle(host, stage, reference.is_some(), error),
        }
    }
    Err("the finisher did not settle".into())
}

fn wait_for_dpkg(host: &mut impl Host, pid: u32, start: u64) -> Result<(), String> {
    for _ in 0..DPKG_EXIT_POLLS {
        if host.process_start(pid) != Some(start) {
            return Ok(());
        }
        host.pause();
    }
    Err("dpkg did not exit".into())
}

/// The digest the restarted helper must run: the ingress digest of an armed
/// candidate. Nothing is re-hashed from disk; with no armed candidate for the
/// installed version (an installer upgrade) there is no reference to prove.
fn reference_digest(host: &mut impl Host) -> Option<String> {
    let version = host
        .run(
            "/usr/bin/dpkg-query",
            &["-W", "-f=${Version}", "vonk-forge-agent"],
        )
        .ok()?;
    host.armed_helper_sha256(&version)
}

fn helper_property(host: &mut impl Host, name: &str) -> Result<String, String> {
    host.run(
        SYSTEMCTL,
        &[
            "--system",
            "show",
            &format!("--property={name}"),
            "--value",
            HELPER_UNIT,
        ],
    )
}

fn advance(
    host: &mut impl Host,
    stage: Stage,
    reference: Option<&str>,
) -> Result<Option<Stage>, String> {
    match stage {
        Stage::StopAgent => {
            let mut arguments = vec!["--system", OperatorActionName::Stop.as_str()];
            arguments.extend(AGENT_UNITS);
            best_effort(host, SYSTEMCTL, &arguments);
            Ok(Some(Stage::RestartHelper))
        }
        Stage::RestartHelper => {
            host.run(SYSTEMCTL, &["--system", "restart", HELPER_UNIT])?;
            host.run(
                SYSTEMCTL,
                &["--system", "is-active", "--quiet", HELPER_UNIT],
            )
            .map_err(|_| "the package helper is not active after its restart")?;
            let helper_pid = helper_property(host, "MainPID")?
                .parse()
                .ok()
                .filter(|pid| *pid > 1)
                .ok_or("the package helper has no main process")?;
            Ok(Some(Stage::ProveHelper { helper_pid }))
        }
        Stage::ProveHelper { helper_pid } => {
            if let Some(expected) = reference {
                let running = host.exe_sha256(helper_pid)?;
                if running != expected {
                    return Err("the running package helper is not the candidate".into());
                }
            }
            Ok(Some(Stage::StartAgent))
        }
        Stage::StartAgent => {
            let mut arguments = vec!["--system", "restart"];
            arguments.extend(AGENT_UNITS);
            host.run(SYSTEMCTL, &arguments)?;
            Ok(None)
        }
    }
}

/// A failed step. While the candidate's activation is armed and the new helper
/// is not proven, the agent stays stopped: nothing it reports can be
/// acknowledged, and the rollback watchdog restores the source at the
/// deadline. Anywhere else the agent is never left stopped.
fn settle(host: &mut impl Host, stage: Stage, armed: bool, error: String) -> Result<(), String> {
    let helper_unproven = matches!(stage, Stage::RestartHelper | Stage::ProveHelper { .. });
    if !(armed && helper_unproven) {
        // A restart also starts a stopped unit.
        let mut arguments = vec!["--system", "restart"];
        arguments.extend(AGENT_UNITS);
        best_effort(host, SYSTEMCTL, &arguments);
    }
    Err(error)
}
