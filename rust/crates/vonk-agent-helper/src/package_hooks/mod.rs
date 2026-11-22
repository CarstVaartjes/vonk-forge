//! Package lifecycle logic. The Debian maintainer scripts are shims that exec
//! `vonk-agent-helper --package-hook <script> <dpkg arguments>`; everything a
//! package does at configure time lives here, behind one [`Host`] so a test
//! runs it against an in-memory machine.
//!
//! A hook runs only where its own binary is on disk (design: `postinst
//! configure` and its aborts, `prerm remove`, plus the transient finisher
//! `configure` schedules).
mod account;
mod directories;
mod finish;
mod host;
mod machine_evidence;
mod removal;
mod subids;
mod units;

#[cfg(test)]
mod tests;

pub use host::{Entry, Host, Kind, SystemHost};

/// What dpkg asks of the package, parsed once from its own argv.
#[derive(Debug, PartialEq, Eq)]
pub enum Hook {
    /// `postinst configure [previous-version]`.
    Configure { previous: Option<String> },
    /// `postinst abort-upgrade|abort-remove|abort-deconfigure`: the shims
    /// leave nothing to undo.
    PostinstAbort,
    /// `prerm remove`: stop and disable what the package runs.
    PrermRemove,
    /// The transient finisher unit `Configure` schedules. Internal. `reference`
    /// is the armed candidate's helper digest, absent for an installer upgrade.
    Finish {
        dpkg_pid: u32,
        dpkg_start: u64,
        reference: Option<String>,
    },
    /// A dpkg action this package has no work for (`triggered`, a future one).
    Unrecognized,
}

impl Hook {
    pub fn parse(arguments: &[String]) -> Result<Self, String> {
        let words: Vec<&str> = arguments.iter().map(String::as_str).collect();
        match words.as_slice() {
            ["postinst", "configure", rest @ ..] => Ok(Self::Configure {
                previous: rest
                    .first()
                    .filter(|version| !version.is_empty())
                    .map(|version| (*version).to_owned()),
            }),
            [
                "postinst",
                "abort-upgrade" | "abort-remove" | "abort-deconfigure",
                ..,
            ] => Ok(Self::PostinstAbort),
            ["postinst", ..] => Ok(Self::Unrecognized),
            ["prerm", "remove", ..] => Ok(Self::PrermRemove),
            ["prerm", ..] => Ok(Self::Unrecognized),
            ["finish", pid, start, rest @ ..] => Ok(Self::Finish {
                dpkg_pid: pid
                    .parse()
                    .map_err(|_| "finisher dpkg pid is not a number")?,
                dpkg_start: start
                    .parse()
                    .map_err(|_| "finisher dpkg start is not a number")?,
                reference: rest.first().map(|digest| (*digest).to_owned()),
            }),
            _ => Err("unknown package hook".into()),
        }
    }
}

/// `--package-hook` arguments to completion.
pub fn run_arguments(arguments: &[String], host: &mut impl Host) -> Result<(), String> {
    run(&Hook::parse(arguments)?, host)
}

pub fn run(hook: &Hook, host: &mut impl Host) -> Result<(), String> {
    match hook {
        Hook::Configure { previous } => configure(previous.is_some(), host),
        Hook::PrermRemove => {
            removal::run(host);
            Ok(())
        }
        Hook::Finish {
            dpkg_pid,
            dpkg_start,
            reference,
        } => finish::run(host, *dpkg_pid, *dpkg_start, reference.as_deref()),
        Hook::PostinstAbort | Hook::Unrecognized => Ok(()),
    }
}

/// Converge the machine to what the package needs. Idempotent: a converged
/// machine takes no write; it still asks systemd to reload and, on an upgrade,
/// to schedule the finisher.
fn configure(upgrade: bool, host: &mut impl Host) -> Result<(), String> {
    let ids = account::ensure(host)?;
    subids::ensure(host, ids.uid)?;
    directories::ensure(host, &ids)?;
    machine_evidence::ensure(host, &ids)?;
    units::enable(host)?;
    units::retire_forward_capsule(host);
    units::reload_and_finish(host, upgrade)
}

/// A step whose failure never blocks dpkg: say why on stderr and go on.
fn best_effort(host: &mut impl Host, program: &str, arguments: &[&str]) {
    if let Err(error) = host.run(program, arguments) {
        eprintln!("vonk-forge-agent: {error}");
    }
}
