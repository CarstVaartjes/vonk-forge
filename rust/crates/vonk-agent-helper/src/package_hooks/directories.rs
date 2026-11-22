use super::Host;
use super::account::Ids;

#[derive(Clone, Copy)]
enum Owner {
    Root,
    Agent,
}

/// Every directory the package owns, parents before children.
const DIRECTORIES: &[(&str, Owner, u32)] = &[
    ("/var/lib/vonk-forge", Owner::Root, 0o755),
    ("/var/lib/vonk-forge-agent", Owner::Agent, 0o700),
    ("/var/lib/vonk-forge-agent/containers", Owner::Agent, 0o750),
    (
        "/var/lib/vonk-forge-agent/image-imports",
        Owner::Agent,
        0o700,
    ),
    (
        "/var/lib/vonk-forge-agent/installations",
        Owner::Agent,
        0o700,
    ),
    (
        "/var/lib/vonk-forge-agent/run-metadata",
        Owner::Agent,
        0o700,
    ),
    ("/var/lib/vonk-forge-agent/runs", Owner::Agent, 0o700),
    ("/var/lib/vonk-forge-agent/tmp", Owner::Agent, 0o700),
    ("/var/lib/vonk-forge/helper", Owner::Root, 0o700),
    ("/var/lib/vonk-forge/helper/requests", Owner::Root, 0o700),
    ("/var/lib/vonk-forge/runtime-images", Owner::Root, 0o700),
    ("/var/lib/vonk-forge/incoming", Owner::Agent, 0o700),
];

/// Create or correct only what differs: an already-converged directory is not
/// written, which an upgrade inside the helper's read-only sandbox relies on.
pub(super) fn ensure(host: &mut impl Host, ids: &Ids) -> Result<(), String> {
    for (path, owner, mode) in DIRECTORIES {
        let (uid, gid) = match owner {
            Owner::Root => (0, 0),
            Owner::Agent => (ids.uid, ids.gid),
        };
        match host.stat(path) {
            Some(entry) if entry.kind != super::Kind::Dir => {
                return Err(format!("{path} is not a directory"));
            }
            Some(entry) if (entry.uid, entry.gid, entry.mode) == (uid, gid, *mode) => {}
            _ => host.make_dir(path, uid, gid, *mode)?,
        }
    }
    Ok(())
}
