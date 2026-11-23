//! Podman needs a standard subordinate range to map both OCI image ownership
//! and the required numeric non-root runtime identity. The first free range
//! of `login.defs` is allocated without disturbing host-managed ranges.
use super::Host;
use super::account::USER;

const LOGIN_DEFS: &str = "/etc/login.defs";
const MINIMUM_COUNT: u64 = 65536;

struct Kind {
    file: &'static str,
    prefix: &'static str,
    option: &'static str,
}

const KINDS: [Kind; 2] = [
    Kind {
        file: "/etc/subuid",
        prefix: "SUB_UID",
        option: "--add-subuids",
    },
    Kind {
        file: "/etc/subgid",
        prefix: "SUB_GID",
        option: "--add-subgids",
    },
];

/// The last numeric value `login.defs` gives `key`.
fn login_value(login_defs: &str, key: &str, fallback: u64) -> u64 {
    login_defs
        .lines()
        .filter_map(|line| {
            let mut fields = line.split_whitespace();
            (fields.next() == Some(key))
                .then(|| fields.next()?.parse().ok())
                .flatten()
        })
        .next_back()
        .unwrap_or(fallback)
}

/// `(start, count)` of every well-formed entry.
fn ranges(file: &str) -> Vec<(u64, u64, &str)> {
    file.lines()
        .filter_map(|line| {
            let mut fields = line.split(':');
            let owner = fields.next()?;
            let start = fields.next()?.parse().ok()?;
            let count = fields.next()?.parse().ok()?;
            Some((start, count, owner))
        })
        .collect()
}

/// The account already holds at least `count` ids.
fn holds(file: &str, uid: u32, count: u64) -> bool {
    let uid = uid.to_string();
    ranges(file)
        .iter()
        .filter(|(_, _, owner)| *owner == USER || *owner == uid)
        .map(|(_, held, _)| held)
        .sum::<u64>()
        >= count
}

/// The first `count` ids in `minimum..=maximum` that no entry overlaps.
fn allocate(file: &str, minimum: u64, maximum: u64, count: u64) -> Option<(u64, u64)> {
    let mut taken: Vec<(u64, u64)> = ranges(file)
        .into_iter()
        .filter(|(_, held, _)| *held > 0)
        .map(|(start, held, _)| (start, start + held))
        .collect();
    taken.sort_unstable();
    let mut candidate = minimum;
    for (start, end) in taken {
        if start >= candidate + count {
            break;
        }
        candidate = candidate.max(end);
    }
    (candidate + count - 1 <= maximum).then_some((candidate, candidate + count - 1))
}

pub(super) fn ensure(host: &mut impl Host, uid: u32) -> Result<(), String> {
    let login_defs = host.read(LOGIN_DEFS).unwrap_or_default();
    for kind in KINDS {
        let minimum = login_value(&login_defs, &format!("{}_MIN", kind.prefix), 100_000);
        let maximum = login_value(&login_defs, &format!("{}_MAX", kind.prefix), 600_100_000);
        let count = login_value(
            &login_defs,
            &format!("{}_COUNT", kind.prefix),
            MINIMUM_COUNT,
        )
        .max(MINIMUM_COUNT);
        let file = match host.lstat(kind.file) {
            Some(_) => host.read(kind.file)?,
            None => {
                host.write_file(kind.file, "", 0, 0, 0o644)?;
                String::new()
            }
        };
        if holds(&file, uid, count) {
            continue;
        }
        let (first, last) = allocate(&file, minimum, maximum, count)
            .ok_or("no subordinate ID range is available")?;
        host.run(
            "/usr/sbin/usermod",
            &[kind.option, &format!("{first}-{last}"), USER],
        )?;
    }
    Ok(())
}

#[cfg(test)]
mod tests;
