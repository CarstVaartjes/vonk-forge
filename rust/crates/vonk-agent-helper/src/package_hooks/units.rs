use super::finish::{AGENT_UNITS, FINISHER_RUNTIME_SECONDS};
use super::{Host, best_effort};

pub(super) const SYSTEMCTL: &str = "/usr/bin/systemctl";
pub(super) const ENABLE: &str = "/usr/bin/deb-systemd-helper";
const HELPER: &str = "/usr/lib/vonk-forge/vonk-agent-helper";
pub(super) const FINISHER_UNIT: &str = "vonk-forge-package-helper-upgrade-finish.service";
const FIREWALL_CONFIG: &str = "/etc/vonk-forge-agent/docker-firewall.conf";

/// How `enable` establishes a unit.
pub(super) enum Enablement {
    /// Never lets the install fail.
    BestEffort,
    /// Boot recovery: an upgrade runs inside the existing helper sandbox,
    /// where Debian's enablement state is read-only, so touch it only when it
    /// is not already established. A fresh install must establish it.
    BootRecovery,
    /// Only when the operator's firewall configuration exists.
    WhenFirewallConfigured,
}

/// One unit of the package. The single list both `enable` and the removal
/// walk, so what install enables is exactly what removal disables.
pub(super) struct Unit {
    pub name: &'static str,
    /// `None`: socket-activated, never enabled by the package.
    pub enablement: Option<Enablement>,
    /// Stopped by removal.
    pub stopped_on_removal: bool,
}

/// In the order removal stops them: the consumers before the helper they use.
pub(super) const UNITS: [Unit; 6] = [
    Unit {
        name: "vonk-forge-agent.service",
        enablement: Some(Enablement::BestEffort),
        stopped_on_removal: true,
    },
    Unit {
        name: "vonk-forge-monitor.service",
        enablement: Some(Enablement::BestEffort),
        stopped_on_removal: true,
    },
    Unit {
        name: "vonk-forge-package-helper.socket",
        enablement: Some(Enablement::BestEffort),
        stopped_on_removal: true,
    },
    Unit {
        name: "vonk-forge-package-helper.service",
        enablement: None,
        stopped_on_removal: true,
    },
    Unit {
        name: "vonk-forge-docker-firewall.service",
        enablement: Some(Enablement::WhenFirewallConfigured),
        stopped_on_removal: true,
    },
    // The rollback watchdog is not stopped by removal yet: it keeps its own
    // deadline.
    Unit {
        name: "vonk-forge-package-rollback.service",
        enablement: Some(Enablement::BootRecovery),
        stopped_on_removal: false,
    },
];

/// Staged by a retired forward-recovery package; dpkg never owned them, and a
/// leftover would keep gating the agent on a runner nothing maintains.
/// Temporary: deleted once both Sparks are verified clean.
const CAPSULE_UNIT: &str = "/lib/systemd/system/vonk-forge-package-upgrade-recover-capsule.service";
const CAPSULE_FILES: [&str; 5] = [
    CAPSULE_UNIT,
    "/lib/systemd/system/multi-user.target.wants/vonk-forge-package-upgrade-recover-capsule.service",
    "/lib/systemd/system/vonk-forge-agent.service.d/10-package-upgrade-capsule.conf",
    "/lib/systemd/system/vonk-forge-package-upgrade-recover.service.d/10-capsule-owner.conf",
    "/lib/systemd/system/vonk-forge-package-helper.socket.d/20-package-upgrade-recovery.conf",
];
const CAPSULE_DIRECTORIES: [&str; 3] = [
    "/lib/systemd/system/vonk-forge-agent.service.d",
    "/lib/systemd/system/vonk-forge-package-upgrade-recover.service.d",
    "/lib/systemd/system/vonk-forge-package-helper.socket.d",
];

pub(super) fn enable(host: &mut impl Host) -> Result<(), String> {
    for unit in &UNITS {
        match unit.enablement {
            None => {}
            Some(Enablement::BestEffort) => best_effort(host, ENABLE, &["enable", unit.name]),
            Some(Enablement::BootRecovery) => {
                if host.run(ENABLE, &["is-enabled", unit.name]).is_err() {
                    host.run(ENABLE, &["enable", unit.name])?;
                }
            }
            Some(Enablement::WhenFirewallConfigured) if host.lstat(FIREWALL_CONFIG).is_some() => {
                best_effort(host, ENABLE, &["enable", unit.name]);
            }
            Some(Enablement::WhenFirewallConfigured) => {}
        }
    }
    Ok(())
}

/// Best effort: a leftover only costs a retry at the next configure.
pub(super) fn retire_forward_capsule(host: &mut impl Host) {
    if host.systemd_live() && host.lstat(CAPSULE_UNIT).is_some() {
        best_effort(
            host,
            SYSTEMCTL,
            &[
                "--system",
                "stop",
                "vonk-forge-package-upgrade-recover-capsule.service",
            ],
        );
    }
    for path in CAPSULE_FILES.iter().chain(&CAPSULE_DIRECTORIES) {
        if host.lstat(path).is_some()
            && let Err(error) = host.remove(path)
        {
            eprintln!("vonk-forge-agent: {error}");
        }
    }
}

/// Reload systemd, and on an upgrade hand the helper and agent restart to a
/// transient unit that outlives dpkg. A failed reload fails the configure:
/// dpkg then marks the package `iF`, no acknowledgement follows, and the
/// rollback watchdog restores the source.
pub(super) fn reload_and_finish(host: &mut impl Host, upgrade: bool) -> Result<(), String> {
    if !host.systemd_live() {
        return Ok(());
    }
    host.run(SYSTEMCTL, &["--system", "daemon-reload"])?;
    if upgrade {
        schedule_finisher(host)?;
    }
    Ok(())
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

fn schedule_finisher(host: &mut impl Host) -> Result<(), String> {
    let dpkg = host.parent_pid();
    let dpkg_start = host
        .process_start(dpkg)
        .ok_or("cannot inspect dpkg for the safe helper restart")?;
    // The finisher of this same upgrade is already waiting.
    if host
        .run(
            SYSTEMCTL,
            &["--system", "is-active", "--quiet", FINISHER_UNIT],
        )
        .is_ok()
    {
        return Ok(());
    }
    // A failed earlier finisher still holds the unit name. Not loaded is the
    // usual answer and needs no report.
    host.run(SYSTEMCTL, &["--system", "reset-failed", FINISHER_UNIT])
        .unwrap_or_default();
    let (pid, start) = (dpkg.to_string(), dpkg_start.to_string());
    let runtime = format!("--property=RuntimeMaxSec={FINISHER_RUNTIME_SECONDS}s");
    let unit = format!("--unit={FINISHER_UNIT}");
    let mut arguments = vec![
        "--quiet",
        "--collect",
        &unit,
        "--property=Type=exec",
        &runtime,
        "--property=NoNewPrivileges=yes",
        "--property=PrivateTmp=yes",
        "--property=ProtectHome=yes",
        "--property=ProtectSystem=strict",
        "--property=IPAddressDeny=any",
        "--property=RestrictAddressFamilies=AF_UNIX",
    ];
    // With no armed candidate nothing else restores the agent, so systemd
    // starts it whenever the finisher ends, however it ends (a kill included).
    // An armed candidate's agent stays stopped until proven; the watchdog heals.
    let agent_start = format!(
        "--property=ExecStopPost={SYSTEMCTL} --no-block start {}",
        AGENT_UNITS.join(" ")
    );
    // One lookup: the finisher is handed the same reference it was scheduled by.
    let reference = reference_digest(host);
    if reference.is_none() {
        arguments.push(&agent_start);
    }
    arguments.extend([HELPER, "--package-hook", "finish", &pid, &start]);
    arguments.extend(reference.as_deref());
    host.run("/usr/bin/systemd-run", &arguments)
        .map(|_| ())
        .map_err(|error| format!("cannot schedule the package finisher: {error}"))
}
