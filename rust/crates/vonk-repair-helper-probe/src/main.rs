#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("vonk-repair-helper-probe: Linux is required");
    std::process::exit(2);
}

#[cfg(target_os = "linux")]
mod linux {
    use sha2::{Digest, Sha256};
    use std::env;
    use std::fmt::Write as _;
    use std::fs::{self, File, Metadata, OpenOptions};
    use std::io::{self, Read};
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    use std::path::{Path, PathBuf};

    const PROBE: &str = "/var/lib/dpkg/tmp.ci/vonk-repair-helper.probe";
    const SETPRIV: &str = "/usr/bin/setpriv";
    const AGENT: &str = "/usr/lib/vonk-forge/vonk-agent";
    const AGENT_CGROUP: &str = "/system.slice/vonk-forge-agent.service";
    const HELPER: &str = "/usr/lib/vonk-forge/vonk-agent-helper";
    const HELPER_CGROUP: &str = "/system.slice/vonk-forge-package-helper.service";
    const CAP_SYS_PTRACE: &str = "0000000000080000";
    const AGENT_INHERITABLE_CAPS: &str = "0000000000200000";
    const AGENT_BOUNDING_CAPS: &str = "00000000002000c2";
    const ZERO_CAPS: &str = "0000000000000000";

    type Result<T> = std::result::Result<T, String>;

    #[derive(Clone, Debug, Eq, PartialEq)]
    struct TargetTuple {
        start: String,
        cgroup: String,
        boot: String,
        exe: PathBuf,
        exe_dev: u64,
        exe_ino: u64,
    }

    fn is_hex(value: &str, length: usize) -> bool {
        value.len() == length
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    }

    fn canonical_u64(value: &str) -> Option<u64> {
        value
            .parse::<u64>()
            .ok()
            .filter(|number| number.to_string() == value)
    }

    fn is_decimal(value: &str) -> bool {
        canonical_u64(value).is_some_and(|number| number > 1)
    }

    fn hash_reader(mut reader: impl Read) -> Result<String> {
        let mut hasher = Sha256::new();
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let count = reader
                .read(&mut buffer)
                .map_err(|error| format!("hash read failed: {error}"))?;
            if count == 0 {
                break;
            }
            hasher.update(&buffer[..count]);
        }
        let mut output = String::with_capacity(64);
        for byte in hasher.finalize() {
            write!(&mut output, "{byte:02x}")
                .map_err(|error| format!("hash formatting failed: {error}"))?;
        }
        Ok(output)
    }

    fn security_xattrs(file: &File, path: &Path) -> Result<Vec<String>> {
        // SAFETY: file is a live descriptor and a null buffer is permitted by
        // flistxattr for its size query.
        let size = unsafe { libc::flistxattr(file.as_raw_fd(), std::ptr::null_mut(), 0) };
        if size < 0 {
            return Err(format!(
                "flistxattr {}: {}",
                path.display(),
                io::Error::last_os_error()
            ));
        }
        if size == 0 {
            return Ok(Vec::new());
        }
        let mut buffer = vec![0_u8; size as usize];
        // SAFETY: buffer is writable for exactly buffer.len() bytes and file is
        // a live descriptor for the duration of the call.
        let written =
            unsafe { libc::flistxattr(file.as_raw_fd(), buffer.as_mut_ptr().cast(), buffer.len()) };
        if written < 0 || written as usize != buffer.len() {
            return Err(format!(
                "flistxattr read {}: {}",
                path.display(),
                io::Error::last_os_error()
            ));
        }
        Ok(buffer
            .split(|byte| *byte == 0)
            .filter(|name| name.starts_with(b"security."))
            .map(|name| String::from_utf8_lossy(name).into_owned())
            .collect())
    }

    fn metadata_identity(
        metadata: &Metadata,
    ) -> (u64, u64, u32, u32, u32, u64, u64, i64, i64, i64, i64) {
        (
            metadata.dev(),
            metadata.ino(),
            metadata.uid(),
            metadata.gid(),
            metadata.mode(),
            metadata.nlink(),
            metadata.len(),
            metadata.mtime(),
            metadata.mtime_nsec(),
            metadata.ctime(),
            metadata.ctime_nsec(),
        )
    }

    fn validate_authority_file(path: &Path, expected_sha: &str) -> Result<()> {
        let before = fs::symlink_metadata(path)
            .map_err(|error| format!("metadata {}: {error}", path.display()))?;
        let mut options = OpenOptions::new();
        options
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NOCTTY);
        let file = options
            .open(path)
            .map_err(|error| format!("open {}: {error}", path.display()))?;
        let held = file
            .metadata()
            .map_err(|error| format!("fstat {}: {error}", path.display()))?;
        if before.file_type().is_symlink()
            || !before.is_file()
            || !held.is_file()
            || held.dev() != before.dev()
            || held.ino() != before.ino()
            || held.uid() != 0
            || held.gid() != 0
            || held.nlink() != 1
            || held.mode() & 0o7777 != 0o755
        {
            return Err(format!("unsafe authority file: {}", path.display()));
        }
        let xattrs = security_xattrs(&file, path)?;
        if !xattrs.is_empty() {
            return Err(format!(
                "authority file has security xattrs: {}: {}",
                path.display(),
                xattrs.join(",")
            ));
        }
        if hash_reader(&file)? != expected_sha {
            return Err(format!("authority digest mismatch: {}", path.display()));
        }
        let after = fs::symlink_metadata(path)
            .map_err(|error| format!("recheck metadata {}: {error}", path.display()))?;
        if metadata_identity(&before) != metadata_identity(&after)
            || metadata_identity(&held) != metadata_identity(&after)
        {
            return Err(format!("authority file changed: {}", path.display()));
        }
        Ok(())
    }

    fn status_value<'a>(status: &'a str, name: &str) -> Result<&'a str> {
        status
            .lines()
            .find_map(|line| line.strip_prefix(name))
            .map(str::trim)
            .ok_or_else(|| format!("missing status field: {name}"))
    }

    fn canonical_groups(status: &str) -> Result<String> {
        let groups = status_value(status, "Groups:")?
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(",");
        Ok(if groups.is_empty() {
            "none".to_string()
        } else {
            groups
        })
    }

    fn validate_self_ids(status: &str, expected_uid: &str, expected_gid: &str) -> Result<()> {
        for (field, expected) in [("Uid:", expected_uid), ("Gid:", expected_gid)] {
            let values: Vec<&str> = status_value(status, field)?.split_whitespace().collect();
            if values != [expected, expected, expected, expected] {
                return Err(format!("unexpected self {field}"));
            }
        }
        Ok(())
    }

    fn validate_sandbox(status: &str) -> Result<()> {
        if status_value(status, "NoNewPrivs:")? != "1"
            || status_value(status, "Seccomp:")? != "2"
            || !canonical_u64(status_value(status, "Seccomp_filters:")?)
                .is_some_and(|count| count >= 1)
        {
            return Err("probe sandbox is not active".to_string());
        }
        Ok(())
    }

    fn validate_helper_probe_self() -> Result<()> {
        let status = fs::read_to_string("/proc/self/status")
            .map_err(|error| format!("self status: {error}"))?;
        validate_self_ids(&status, "0", "0")?;
        for field in ["CapInh:", "CapAmb:"] {
            if status_value(&status, field)? != ZERO_CAPS {
                return Err(format!("unexpected self {field}"));
            }
        }
        for field in ["CapPrm:", "CapEff:", "CapBnd:"] {
            if status_value(&status, field)? != CAP_SYS_PTRACE {
                return Err(format!("unexpected self {field}"));
            }
        }
        validate_sandbox(&status)
    }

    fn validate_agent_probe_self(
        expected_uid: &str,
        expected_gid: &str,
        expected_groups: &str,
    ) -> Result<()> {
        let status = fs::read_to_string("/proc/self/status")
            .map_err(|error| format!("self status: {error}"))?;
        validate_self_ids(&status, expected_uid, expected_gid)?;
        let groups = canonical_groups(&status)?;
        if groups != expected_groups {
            return Err("unexpected self supplementary groups".to_string());
        }
        for field in ["CapInh:", "CapPrm:", "CapEff:", "CapBnd:", "CapAmb:"] {
            if status_value(&status, field)? != ZERO_CAPS {
                return Err(format!("unexpected self {field}"));
            }
        }
        validate_sandbox(&status)
    }

    fn read_boot_id() -> Result<String> {
        let value = fs::read_to_string("/proc/sys/kernel/random/boot_id")
            .map_err(|error| format!("boot id: {error}"))?;
        let value = value.trim().to_ascii_lowercase();
        if value.len() != 36
            || !value.bytes().enumerate().all(|(index, byte)| match index {
                8 | 13 | 18 | 23 => byte == b'-',
                _ => byte.is_ascii_hexdigit(),
            })
        {
            return Err("invalid boot id".to_string());
        }
        Ok(value)
    }

    fn read_start(pid: &str) -> Result<String> {
        let stat = fs::read_to_string(format!("/proc/{pid}/stat"))
            .map_err(|error| format!("target stat: {error}"))?;
        let suffix = stat
            .rsplit_once(") ")
            .ok_or_else(|| "invalid target stat".to_string())?
            .1;
        let fields: Vec<&str> = suffix.split_whitespace().collect();
        let start = fields
            .get(19)
            .ok_or_else(|| "target start missing".to_string())?;
        if !is_decimal(start) {
            return Err("target start invalid".to_string());
        }
        Ok((*start).to_string())
    }

    fn validate_target_ids(pid: &str, expected_uid: &str, expected_gid: &str) -> Result<String> {
        let status = fs::read_to_string(format!("/proc/{pid}/status"))
            .map_err(|error| format!("target status: {error}"))?;
        for (field, expected) in [("Uid:", expected_uid), ("Gid:", expected_gid)] {
            let values: Vec<&str> = status_value(&status, field)?.split_whitespace().collect();
            if values != [expected, expected, expected, expected] {
                return Err(format!("unexpected target {field}"));
            }
        }
        Ok(status)
    }

    fn read_cgroup(pid: &str, expected_cgroup: &str) -> Result<String> {
        let raw = fs::read_to_string(format!("/proc/{pid}/cgroup"))
            .map_err(|error| format!("target cgroup: {error}"))?;
        let expected = format!("0::{expected_cgroup}\n");
        if raw != expected {
            return Err("unexpected target cgroup".to_string());
        }
        Ok(expected_cgroup.to_string())
    }

    fn capture_target(
        pid: &str,
        expected_uid: &str,
        expected_gid: &str,
        expected_path: &str,
        expected_cgroup: &str,
    ) -> Result<TargetTuple> {
        validate_target_ids(pid, expected_uid, expected_gid)?;
        let start = read_start(pid)?;
        let cgroup = read_cgroup(pid, expected_cgroup)?;
        let boot = read_boot_id()?;
        let exe_link = PathBuf::from(format!("/proc/{pid}/exe"));
        let exe = fs::read_link(&exe_link).map_err(|error| format!("target exe link: {error}"))?;
        if exe != Path::new(expected_path) || exe.as_os_str().as_bytes().ends_with(b" (deleted)") {
            return Err("unexpected target executable".to_string());
        }
        let metadata =
            fs::metadata(&exe_link).map_err(|error| format!("target exe metadata: {error}"))?;
        if !metadata.is_file() {
            return Err("target executable is not regular".to_string());
        }
        Ok(TargetTuple {
            start,
            cgroup,
            boot,
            exe,
            exe_dev: metadata.dev(),
            exe_ino: metadata.ino(),
        })
    }

    fn validate_agent_target_security(pid: &str, expected_groups: &str) -> Result<()> {
        let status = fs::read_to_string(format!("/proc/{pid}/status"))
            .map_err(|error| format!("target status: {error}"))?;
        let groups = canonical_groups(&status)?;
        if groups != expected_groups {
            return Err("unexpected target supplementary groups".to_string());
        }
        for field in ["CapPrm:", "CapEff:", "CapAmb:"] {
            if status_value(&status, field)? != ZERO_CAPS {
                return Err(format!("unexpected target {field}"));
            }
        }
        if status_value(&status, "CapInh:")? != AGENT_INHERITABLE_CAPS {
            return Err("unexpected target CapInh:".to_string());
        }
        if status_value(&status, "CapBnd:")? != AGENT_BOUNDING_CAPS {
            return Err("unexpected target CapBnd:".to_string());
        }
        Ok(())
    }

    fn check_wrapper(args: &[String]) -> Result<()> {
        if args.len() != 2 || !is_hex(&args[0], 64) || !is_hex(&args[1], 64) {
            return Err("invalid check-wrapper arguments".to_string());
        }
        validate_authority_file(Path::new(SETPRIV), &args[0])?;
        validate_authority_file(Path::new(PROBE), &args[1])?;
        println!(
            "schema_version=1 setpriv_sha256={} probe_sha256={}",
            args[0], args[1]
        );
        Ok(())
    }

    fn probe_helper(args: &[String]) -> Result<()> {
        if args.len() != 9
            || !is_decimal(&args[0])
            || args[0] == "0"
            || args[0] == "1"
            || !is_decimal(&args[1])
            || !is_hex(&args[2], 64)
            || !is_hex(&args[3], 64)
            || !is_hex(&args[4], 64)
            || args[5].len() != 36
            || !is_hex(&args[6], 32)
            || !is_hex(&args[7], 64)
            || !is_hex(&args[8], 64)
        {
            return Err("invalid probe-helper arguments".to_string());
        }
        // SAFETY: alarm only bounds this short-lived process and installs no handler.
        unsafe { libc::alarm(5) };
        validate_helper_probe_self()?;
        validate_authority_file(Path::new(SETPRIV), &args[7])?;
        validate_authority_file(Path::new(PROBE), &args[8])?;

        let before = capture_target(&args[0], "0", "0", HELPER, HELPER_CGROUP)?;
        if before.start != args[1] || before.boot != args[5] {
            return Err("target tuple does not match authority".to_string());
        }
        let exe_link = PathBuf::from(format!("/proc/{}/exe", args[0]));
        let held = File::open(&exe_link).map_err(|error| format!("open target exe: {error}"))?;
        let held_metadata = held
            .metadata()
            .map_err(|error| format!("held target metadata: {error}"))?;
        if held_metadata.dev() != before.exe_dev || held_metadata.ino() != before.exe_ino {
            return Err("target executable changed before hold".to_string());
        }
        let digest = hash_reader(&held)?;
        if digest != args[4] {
            return Err("target helper digest mismatch".to_string());
        }
        let after = capture_target(&args[0], "0", "0", HELPER, HELPER_CGROUP)?;
        if before != after {
            return Err("target tuple changed during probe".to_string());
        }
        println!(
            "schema_version=1 nonce={} authority_sha256={} helper_pid={} helper_start={} helper_sha256={} boot_id={} invocation_id={} exe_dev={} exe_ino={} cap_eff=0000000000080000 cap_ambient=0000000000000000 no_new_privs=1 seccomp=2",
            args[2],
            args[3],
            args[0],
            args[1],
            digest,
            args[5],
            args[6],
            before.exe_dev,
            before.exe_ino
        );
        Ok(())
    }

    fn probe_agent(args: &[String]) -> Result<()> {
        if args.len() != 12
            || !is_decimal(&args[0])
            || !is_decimal(&args[1])
            || !is_hex(&args[2], 64)
            || !is_hex(&args[3], 64)
            || !is_hex(&args[4], 64)
            || args[5].len() != 36
            || !is_hex(&args[6], 32)
            || !is_decimal(&args[7])
            || !is_decimal(&args[8])
            || (args[9] != "none" && args[9] != args[8])
            || !is_hex(&args[10], 64)
            || !is_hex(&args[11], 64)
        {
            return Err("invalid probe-agent arguments".to_string());
        }
        // SAFETY: alarm only bounds this short-lived process and installs no handler.
        unsafe { libc::alarm(5) };
        validate_agent_probe_self(&args[7], &args[8], &args[9])?;
        validate_authority_file(Path::new(SETPRIV), &args[10])?;
        validate_authority_file(Path::new(PROBE), &args[11])?;

        validate_agent_target_security(&args[0], &args[9])?;
        let before = capture_target(&args[0], &args[7], &args[8], AGENT, AGENT_CGROUP)?;
        if before.start != args[1] || before.boot != args[5] {
            return Err("target tuple does not match authority".to_string());
        }
        let exe_link = PathBuf::from(format!("/proc/{}/exe", args[0]));
        let held = File::open(&exe_link).map_err(|error| format!("open target exe: {error}"))?;
        let held_metadata = held
            .metadata()
            .map_err(|error| format!("held target metadata: {error}"))?;
        if held_metadata.dev() != before.exe_dev || held_metadata.ino() != before.exe_ino {
            return Err("target executable changed before hold".to_string());
        }
        let digest = hash_reader(&held)?;
        if digest != args[4] {
            return Err("target agent digest mismatch".to_string());
        }
        validate_agent_target_security(&args[0], &args[9])?;
        let after = capture_target(&args[0], &args[7], &args[8], AGENT, AGENT_CGROUP)?;
        if before != after {
            return Err("target tuple changed during probe".to_string());
        }
        println!(
            "schema_version=1 nonce={} authority_sha256={} agent_pid={} agent_start={} agent_sha256={} boot_id={} invocation_id={} agent_uid={} agent_gid={} agent_groups={} exe_dev={} exe_ino={} cap_eff=0000000000000000 cap_ambient=0000000000000000 no_new_privs=1 seccomp=2",
            args[2],
            args[3],
            args[0],
            args[1],
            digest,
            args[5],
            args[6],
            args[7],
            args[8],
            args[9],
            before.exe_dev,
            before.exe_ino
        );
        Ok(())
    }

    pub fn run() -> Result<()> {
        let mut args = env::args();
        let _program = args.next();
        let command = args.next().ok_or_else(|| "missing command".to_string())?;
        let rest: Vec<String> = args.collect();
        match command.as_str() {
            "check-wrapper" => check_wrapper(&rest),
            "probe-helper" => probe_helper(&rest),
            "probe-agent" => probe_agent(&rest),
            _ => Err("unsupported command".to_string()),
        }
    }
}

#[cfg(target_os = "linux")]
fn main() {
    if let Err(error) = linux::run() {
        eprintln!("vonk-repair-helper-probe: {error}");
        std::process::exit(2);
    }
}
