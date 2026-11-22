use vonk_agent_protocol::generated::OperatorActionName;

use super::{Host, best_effort};

const SYSTEMCTL: &str = "/usr/bin/systemctl";
const ENABLE: &str = "/usr/bin/deb-systemd-helper";
const HELPER: &str = "/usr/lib/vonk-forge/vonk-agent-helper";
pub(super) const FINISHER_UNIT: &str = "vonk-forge-package-helper-upgrade-finish.service";
const FIREWALL_CONFIG: &str = "/etc/vonk-forge-agent/docker-firewall.conf";
const ROLLBACK_UNIT: &str = "vonk-forge-package-rollback.service";

/// Units whose enablement the package never lets fail the install.
const OPTIONAL_UNITS: [&str; 3] = [
    "vonk-forge-package-helper.socket",
    "vonk-forge-agent.service",
    "vonk-forge-monitor.service",
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
    for unit in &OPTIONAL_UNITS[..1] {
        best_effort(host, ENABLE, &["enable", unit]);
    }
    // An upgrade runs inside the existing helper sandbox, where Debian's
    // enablement state is read-only: touch it only when boot recovery is not
    // already established. A fresh install must establish it.
    if host.run(ENABLE, &["is-enabled", ROLLBACK_UNIT]).is_err() {
        host.run(ENABLE, &["enable", ROLLBACK_UNIT])?;
    }
    for unit in &OPTIONAL_UNITS[1..] {
        best_effort(host, ENABLE, &["enable", unit]);
    }
    if host.lstat(FIREWALL_CONFIG).is_some() {
        best_effort(
            host,
            ENABLE,
            &["enable", "vonk-forge-docker-firewall.service"],
        );
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
                OperatorActionName::Stop.as_str(),
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
    // A failed earlier finisher still holds the unit name.
    best_effort(
        host,
        SYSTEMCTL,
        &["--system", "reset-failed", FINISHER_UNIT],
    );
    let (pid, start) = (dpkg.to_string(), dpkg_start.to_string());
    host.run(
        "/usr/bin/systemd-run",
        &[
            "--quiet",
            "--collect",
            &format!("--unit={FINISHER_UNIT}"),
            "--property=Type=exec",
            "--property=RuntimeMaxSec=240s",
            "--property=NoNewPrivileges=yes",
            "--property=PrivateTmp=yes",
            "--property=ProtectHome=yes",
            "--property=ProtectSystem=strict",
            "--property=IPAddressDeny=any",
            "--property=RestrictAddressFamilies=AF_UNIX",
            HELPER,
            "--package-hook",
            "finish",
            &pid,
            &start,
        ],
    )
    .map(|_| ())
    .map_err(|error| format!("cannot schedule the package finisher: {error}"))
}
