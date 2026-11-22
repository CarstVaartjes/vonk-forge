//! `prerm remove`: stop what the package runs and take it out of boot, so a
//! removed package leaves no service behind. Best effort throughout: removal
//! never fails, whatever state the machine is in.
use super::account::USER;
use super::units::{ENABLE, FINISHER_UNIT, SYSTEMCTL, UNITS};
use super::{Host, best_effort};

const INVOKE: &str = "/usr/bin/deb-systemd-invoke";

pub(super) fn run(host: &mut impl Host) {
    // An upgrade finisher still waiting must not restart what is removed. Not
    // loaded is the usual answer and needs no report.
    host.run(SYSTEMCTL, &["--system", "stop", FINISHER_UNIT])
        .unwrap_or_default();
    for unit in UNITS.iter().filter(|unit| unit.stopped_on_removal) {
        best_effort(host, INVOKE, &["stop", unit.name]);
    }
    for unit in UNITS.iter().filter(|unit| unit.enablement.is_some()) {
        best_effort(host, ENABLE, &["disable", unit.name]);
    }
    // The clock ceiling holds to the next boot: no workload is disturbed.
    best_effort(host, "/usr/bin/loginctl", &["disable-linger", USER]);
}
