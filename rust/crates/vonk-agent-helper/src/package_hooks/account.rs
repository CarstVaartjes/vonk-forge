use super::Host;

pub(super) const USER: &str = "vonk-agent";
const PASSWD: &str = "/etc/passwd";
const GROUP: &str = "/etc/group";
const LINGER: &str = "/var/lib/systemd/linger/vonk-agent";

/// The account's numeric identity.
pub(super) struct Ids {
    pub uid: u32,
    pub gid: u32,
}

/// The id in field three of the first `/etc/passwd` or `/etc/group` line for
/// `name`.
fn lookup(table: &str, name: &str) -> Option<u32> {
    table.lines().find_map(|line| {
        let mut fields = line.split(':');
        (fields.next() == Some(name))
            .then(|| fields.nth(1)?.parse().ok())
            .flatten()
    })
}

pub(super) fn ids(host: &impl Host) -> Option<Ids> {
    Some(Ids {
        uid: lookup(&host.read(PASSWD).ok()?, USER)?,
        gid: lookup(&host.read(GROUP).ok()?, USER)?,
    })
}

/// The system group and user, and the linger the rootless Podman manager needs.
pub(super) fn ensure(host: &mut impl Host) -> Result<Ids, String> {
    if lookup(&host.read(GROUP)?, USER).is_none() {
        host.run("/usr/sbin/addgroup", &["--system", USER])?;
    }
    if lookup(&host.read(PASSWD)?, USER).is_none() {
        host.run(
            "/usr/sbin/adduser",
            &[
                "--system",
                "--ingroup",
                USER,
                "--home",
                "/var/lib/vonk-forge-agent",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                USER,
            ],
        )?;
    }
    if host.lstat(LINGER).is_none() {
        host.run("/usr/bin/loginctl", &["enable-linger", USER])?;
    }
    ids(host).ok_or_else(|| format!("account {USER} is missing after creation"))
}
