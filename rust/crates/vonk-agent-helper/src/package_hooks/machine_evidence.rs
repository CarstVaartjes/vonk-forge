use super::account::Ids;
use super::{Host, Kind};

const PATH: &str = "/var/lib/vonk-forge-agent/machine-evidence";

fn well_formed(text: &str) -> bool {
    text.strip_suffix('\n').is_some_and(|line| {
        line.len() == 64 && line.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    })
}

/// Root-owned, group-readable, stable per machine. Evidence of any other
/// shape is never reused: a regular file or symlink is discarded and
/// regenerated; anything else is unsafe and fails the configure.
pub(super) fn ensure(host: &mut impl Host, ids: &Ids) -> Result<(), String> {
    if let Some(entry) = host.lstat(PATH) {
        let valid = entry.kind == Kind::File
            && (entry.uid, entry.gid, entry.mode) == (0, ids.gid, 0o640)
            && host.read(PATH).is_ok_and(|text| well_formed(&text));
        if valid {
            return Ok(());
        }
        if !matches!(entry.kind, Kind::File | Kind::Symlink) {
            return Err("native machine evidence is unsafe".into());
        }
        eprintln!("vonk-forge-agent: discarded malformed native machine evidence");
        host.remove(PATH)?;
    }
    let evidence = format!("{}\n", host.random_hex());
    host.write_file(PATH, &evidence, 0, ids.gid, 0o640)
}
