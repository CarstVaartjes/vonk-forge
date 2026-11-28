use std::collections::BTreeMap;

use super::*;

const AGENT_DIR: &str = "/var/lib/vonk-forge-agent";
const EVIDENCE: &str = "/var/lib/vonk-forge-agent/machine-evidence";
const HELPER_UNIT: &str = "vonk-forge-package-helper.service";
const NEW_HELPER: &str = "aa";

/// An in-memory machine: files and directories, a command log, no clock.
#[derive(Default)]
struct Fake {
    nodes: BTreeMap<String, (Entry, String)>,
    commands: Vec<String>,
    failing: Vec<&'static str>,
    live: bool,
    dpkg_alive: bool,
    dpkg_exits_after: u32,
    armed: Option<String>,
    helper_exe: String,
    active: Vec<String>,
    show: BTreeMap<String, String>,
}

fn node(kind: Kind, uid: u32, gid: u32, mode: u32) -> Entry {
    Entry {
        kind,
        uid,
        gid,
        mode,
    }
}

impl Fake {
    fn machine() -> Self {
        let mut fake = Fake {
            live: true,
            dpkg_alive: true,
            helper_exe: NEW_HELPER.into(),
            ..Fake::default()
        };
        fake.put("/etc/passwd", "", node(Kind::File, 0, 0, 0o644));
        fake.put("/etc/group", "", node(Kind::File, 0, 0, 0o644));
        fake.put("/etc/login.defs", "", node(Kind::File, 0, 0, 0o644));
        fake.active.push(HELPER_UNIT.into());
        fake.show
            .insert(format!("MainPID {HELPER_UNIT}"), "4242".into());
        fake
    }
    fn put(&mut self, path: &str, text: &str, entry: Entry) {
        self.nodes.insert(path.into(), (entry, text.into()));
    }
    fn ran(&self, needle: &str) -> bool {
        self.commands.iter().any(|command| command.contains(needle))
    }
    fn position(&self, needle: &str) -> usize {
        self.commands
            .iter()
            .position(|command| command.contains(needle))
            .unwrap_or_else(|| panic!("{needle} not run: {:?}", self.commands))
    }
}

impl Host for Fake {
    fn lstat(&self, path: &str) -> Option<Entry> {
        self.nodes.get(path).map(|(entry, _)| *entry)
    }
    fn stat(&self, path: &str) -> Option<Entry> {
        self.lstat(path)
    }
    fn read(&self, path: &str) -> Result<String, String> {
        self.nodes
            .get(path)
            .map(|(_, text)| text.clone())
            .ok_or_else(|| format!("{path}: not found"))
    }
    fn make_dir(&mut self, path: &str, uid: u32, gid: u32, mode: u32) -> Result<(), String> {
        self.commands.push(format!("mkdir {path}"));
        self.put(path, "", node(Kind::Dir, uid, gid, mode));
        Ok(())
    }
    fn write_file(
        &mut self,
        path: &str,
        contents: &str,
        uid: u32,
        gid: u32,
        mode: u32,
    ) -> Result<(), String> {
        self.commands.push(format!("write {path}"));
        self.put(path, contents, node(Kind::File, uid, gid, mode));
        Ok(())
    }
    fn remove(&mut self, path: &str) -> Result<(), String> {
        self.commands.push(format!("remove {path}"));
        self.nodes.remove(path);
        Ok(())
    }
    fn run(&mut self, program: &str, arguments: &[&str]) -> Result<String, String> {
        let line = format!("{program} {}", arguments.join(" "));
        let is_show = arguments.contains(&"show");
        if arguments.contains(&"is-active") {
            let unit = arguments.last().copied().unwrap_or_default();
            return if self.active.iter().any(|active| active == unit) {
                Ok(String::new())
            } else {
                Err(format!("{unit} is not active"))
            };
        }
        if !is_show {
            self.commands.push(line.clone());
        }
        if self.failing.iter().any(|needle| line.contains(needle)) {
            return Err(format!("{line} failed"));
        }
        if program.ends_with("addgroup") {
            self.put(
                "/etc/group",
                "vonk-agent:x:990:\n",
                node(Kind::File, 0, 0, 0o644),
            );
        }
        if program.ends_with("adduser") {
            self.put(
                "/etc/passwd",
                "vonk-agent:x:991:990::/x:/n\n",
                node(Kind::File, 0, 0, 0o644),
            );
        }
        if program.ends_with("loginctl") {
            self.put(
                "/var/lib/systemd/linger/vonk-agent",
                "",
                node(Kind::File, 0, 0, 0o644),
            );
        }
        if program.ends_with("usermod") {
            // The allocated range is now the account's.
            let file = if line.contains("subuids") {
                "/etc/subuid"
            } else {
                "/etc/subgid"
            };
            self.put(
                file,
                "vonk-agent:100000:65536\n",
                node(Kind::File, 0, 0, 0o644),
            );
        }
        if program.ends_with("deb-systemd-helper") && line.contains("is-enabled") {
            return Ok(String::new());
        }
        if program.ends_with("dpkg-query") {
            return Ok("2.0".into());
        }
        if is_show {
            let property = arguments
                .iter()
                .find_map(|argument| argument.strip_prefix("--property="))
                .unwrap_or_default();
            let unit = arguments.last().copied().unwrap_or_default();
            return Ok(self
                .show
                .get(&format!("{property} {unit}"))
                .cloned()
                .unwrap_or_default());
        }
        Ok(String::new())
    }
    fn random_hex(&mut self) -> String {
        "0123456789abcdef".repeat(4)
    }
    fn systemd_live(&self) -> bool {
        self.live
    }
    fn parent_pid(&self) -> u32 {
        77
    }
    fn process_start(&self, pid: u32) -> Option<u64> {
        (pid == 77 && self.dpkg_alive).then_some(5)
    }
    fn exe_sha256(&self, _pid: u32) -> Result<String, String> {
        Ok(self.helper_exe.clone())
    }
    fn armed_helper_sha256(&self, installed_version: &str) -> Option<String> {
        (installed_version == "2.0")
            .then(|| self.armed.clone())
            .flatten()
    }
    fn retire_rollback(&mut self) -> Result<(), String> {
        self.commands.push("retire-rollback".into());
        Ok(())
    }
    fn pause(&mut self) {
        if self.dpkg_exits_after > 0 {
            self.dpkg_exits_after -= 1;
            self.dpkg_alive = self.dpkg_exits_after > 0;
        }
    }
}

fn configure(previous: Option<&str>, fake: &mut Fake) -> Result<(), String> {
    run(
        &Hook::Configure {
            previous: previous.map(str::to_owned),
        },
        fake,
    )
}

#[test]
fn dpkg_arguments_parse_into_one_closed_hook() {
    let parse = |words: &[&str]| {
        Hook::parse(
            &words
                .iter()
                .map(|word| (*word).to_owned())
                .collect::<Vec<_>>(),
        )
    };
    assert_eq!(
        parse(&["postinst", "configure"]),
        Ok(Hook::Configure { previous: None })
    );
    assert_eq!(
        parse(&["postinst", "configure", ""]),
        Ok(Hook::Configure { previous: None })
    );
    assert_eq!(
        parse(&["postinst", "configure", "1.0"]),
        Ok(Hook::Configure {
            previous: Some("1.0".into())
        })
    );
    for abort in ["abort-upgrade", "abort-remove", "abort-deconfigure"] {
        assert_eq!(parse(&["postinst", abort, "1.0"]), Ok(Hook::PostinstAbort));
    }
    assert_eq!(
        parse(&["postinst", "triggered", "x"]),
        Ok(Hook::Unrecognized)
    );
    assert_eq!(
        parse(&["finish", "12", "34"]),
        Ok(Hook::Finish {
            dpkg_pid: 12,
            dpkg_start: 34,
            reference: None
        })
    );
    assert_eq!(
        parse(&["finish", "12", "34", "ab"]),
        Ok(Hook::Finish {
            dpkg_pid: 12,
            dpkg_start: 34,
            reference: Some("ab".into())
        })
    );
    assert!(parse(&["finish", "x", "34"]).is_err());
    assert_eq!(parse(&["prerm", "remove"]), Ok(Hook::PrermRemove));
    assert_eq!(parse(&["prerm", "upgrade", "2.0"]), Ok(Hook::Unrecognized));
    assert!(parse(&["preinst", "install"]).is_err());
}

#[test]
fn configure_converges_a_bare_machine_and_then_does_nothing() {
    let mut fake = Fake::machine();
    configure(None, &mut fake).unwrap();
    assert!(fake.ran("addgroup") && fake.ran("adduser") && fake.ran("enable-linger"));
    assert!(fake.ran("--add-subuids 100000-165535") && fake.ran("--add-subgids"));
    assert_eq!(
        fake.read(EVIDENCE).unwrap(),
        format!("{}\n", "0123456789abcdef".repeat(4))
    );
    let evidence = fake.lstat(EVIDENCE).unwrap();
    assert_eq!((evidence.uid, evidence.gid, evidence.mode), (0, 990, 0o640));
    assert_eq!(fake.stat(AGENT_DIR).unwrap().uid, 991);
    assert!(
        !fake.ran("systemd-run"),
        "a fresh install schedules no finisher"
    );

    fake.commands.clear();
    // The directories were created by this machine; the second pass is a no-op
    // except for what an enabled unit set always costs.
    configure(None, &mut fake).unwrap();
    let effects: Vec<_> = fake
        .commands
        .iter()
        .filter(|command| {
            command.starts_with("mkdir")
                || command.starts_with("write")
                || command.starts_with("remove")
        })
        .collect();
    assert!(effects.is_empty(), "{effects:?}");
    assert!(!fake.ran("addgroup") && !fake.ran("usermod") && !fake.ran("linger"));
}

#[test]
fn machine_evidence_of_the_wrong_shape_is_replaced_or_refused() {
    for kind in [Kind::File, Kind::Symlink] {
        let mut fake = Fake::machine();
        configure(None, &mut fake).unwrap();
        fake.put(EVIDENCE, "not hex\n", node(kind, 0, 990, 0o640));
        fake.commands.clear();
        configure(None, &mut fake).unwrap();
        assert!(fake.ran(&format!("remove {EVIDENCE}")));
        assert!(fake.read(EVIDENCE).unwrap().starts_with("0123"));
    }
    let mut fake = Fake::machine();
    configure(None, &mut fake).unwrap();
    fake.put(EVIDENCE, "", node(Kind::Dir, 0, 990, 0o750));
    assert!(configure(None, &mut fake).is_err());
}

#[test]
fn an_unreloadable_systemd_fails_the_configure_but_an_offline_one_does_not() {
    let mut fake = Fake::machine();
    fake.failing.push("daemon-reload");
    assert!(configure(Some("1.0"), &mut fake).is_err());
    assert!(!fake.ran("systemd-run"));

    let mut fake = Fake::machine();
    fake.live = false;
    configure(Some("1.0"), &mut fake).unwrap();
    assert!(!fake.ran("daemon-reload") && !fake.ran("systemd-run"));
}

#[test]
fn an_upgrade_schedules_the_new_helper_as_the_finisher_after_the_reload() {
    let mut fake = Fake::machine();
    configure(Some("1.0"), &mut fake).unwrap();
    assert!(fake.position("daemon-reload") < fake.position("systemd-run"));
    assert!(fake.ran("/usr/lib/vonk-forge/vonk-agent-helper --package-hook finish 77 5"));
}

#[test]
fn a_missing_subordinate_range_fails_the_configure() {
    let mut fake = Fake::machine();
    fake.put(
        "/etc/login.defs",
        "SUB_UID_MAX 165535\n",
        node(Kind::File, 0, 0, 0o644),
    );
    fake.put(
        "/etc/subuid",
        "host:100000:65536\n",
        node(Kind::File, 0, 0, 0o644),
    );
    assert!(configure(None, &mut fake).is_err());
}

/// The finisher as `Configure` schedules it: handed the reference digest.
fn finish(fake: &mut Fake) -> Result<(), String> {
    let reference = fake.armed.clone();
    run(
        &Hook::Finish {
            dpkg_pid: 77,
            dpkg_start: 5,
            reference,
        },
        fake,
    )
}

#[test]
fn the_finisher_stops_the_agent_before_the_helper_and_restarts_it_proven() {
    let mut fake = Fake::machine();
    fake.dpkg_exits_after = 3;
    fake.armed = Some(NEW_HELPER.into());
    finish(&mut fake).unwrap();
    assert!(!fake.dpkg_alive, "waited for dpkg to exit");
    let stop = fake.position("stop vonk-forge-agent.service");
    let helper = fake.position(&format!("restart {HELPER_UNIT}"));
    let start = fake.position("restart vonk-forge-agent.service");
    assert!(stop < helper && helper < start);
}

#[test]
fn an_unproven_helper_leaves_the_armed_candidate_agent_stopped() {
    let mut fake = Fake::machine();
    fake.dpkg_alive = false;
    fake.armed = Some("not-what-runs".into());
    assert!(finish(&mut fake).is_err());
    assert!(!fake.ran("restart vonk-forge-agent"));
}

#[test]
fn without_an_armed_candidate_a_failed_restart_never_leaves_the_agent_stopped() {
    let mut fake = Fake::machine();
    fake.dpkg_alive = false;
    fake.failing.push("restart vonk-forge-package-helper");
    assert!(finish(&mut fake).is_err());
    assert!(fake.ran(" start vonk-forge-agent"));
}

#[test]
fn an_installer_upgrade_has_no_reference_to_prove() {
    let mut fake = Fake::machine();
    fake.dpkg_alive = false;
    fake.helper_exe = "anything".into();
    finish(&mut fake).unwrap();
    assert!(fake.ran("restart vonk-forge-agent"));
}

#[test]
fn only_an_unarmed_finisher_restarts_the_agent_when_it_ends() {
    for (armed, expected) in [(None, true), (Some(NEW_HELPER.to_owned()), false)] {
        let mut fake = Fake::machine();
        fake.armed = armed;
        configure(Some("1.0"), &mut fake).unwrap();
        let scheduled = fake
            .commands
            .iter()
            .find(|command| command.contains("systemd-run"))
            .unwrap();
        assert_eq!(scheduled.contains("ExecStopPost="), expected, "{scheduled}");
    }
}

#[test]
fn a_special_mode_bit_on_an_existing_directory_is_not_a_difference() {
    let mut fake = Fake::machine();
    configure(None, &mut fake).unwrap();
    fake.put(AGENT_DIR, "", node(Kind::Dir, 991, 990, 0o2700));
    fake.commands.clear();
    configure(None, &mut fake).unwrap();
    assert!(!fake.ran(&format!("mkdir {AGENT_DIR}")));
}

#[test]
fn the_scheduled_finisher_carries_the_reference_it_was_scheduled_by() {
    let mut fake = Fake::machine();
    fake.armed = Some(NEW_HELPER.into());
    configure(Some("1.0"), &mut fake).unwrap();
    assert!(fake.ran(&format!("--package-hook finish 77 5 {NEW_HELPER}")));
    assert_eq!(
        fake.commands
            .iter()
            .filter(|command| command.contains("dpkg-query"))
            .count(),
        1,
        "one lookup"
    );
}

fn units_named(fake: &Fake, program: &str, verb: &str) -> Vec<String> {
    let prefix = format!("{program} {verb} ");
    let mut units: Vec<String> = fake
        .commands
        .iter()
        .filter_map(|command| command.strip_prefix(&prefix))
        .map(str::to_owned)
        .collect();
    units.sort();
    units
}

fn remove(fake: &mut Fake) {
    run(&Hook::PrermRemove, fake).unwrap();
}

#[test]
fn removal_stops_the_consumers_first_and_disables_exactly_what_install_enabled() {
    let mut fake = Fake::machine();
    fake.put(
        "/etc/vonk-forge-agent/docker-firewall.conf",
        "",
        node(Kind::File, 0, 0, 0o644),
    );
    // A fresh machine has no boot recovery yet, so configure enables every unit.
    fake.failing.push("is-enabled");
    configure(None, &mut fake).unwrap();
    let enabled = units_named(&fake, "/usr/bin/deb-systemd-helper", "enable");
    assert_eq!(enabled.len(), 5, "{enabled:?}");

    fake.commands.clear();
    remove(&mut fake);
    assert_eq!(
        units_named(&fake, "/usr/bin/deb-systemd-helper", "disable"),
        enabled
    );
    let stop = |unit: &str| fake.position(&format!("deb-systemd-invoke stop {unit}"));
    assert!(stop("vonk-forge-agent.service") < stop("vonk-forge-package-helper.socket"));
    assert!(stop("vonk-forge-monitor.service") < stop("vonk-forge-package-helper.service"));
    assert!(fake.ran("stop vonk-forge-docker-firewall.service"));
    assert!(fake.ran("stop vonk-forge-package-helper-upgrade-finish.service"));
    assert!(fake.ran("loginctl disable-linger vonk-agent"));
}

#[test]
fn removal_stops_the_watchdog_before_retiring_the_armed_transaction() {
    let mut fake = Fake::machine();
    remove(&mut fake);
    assert!(
        fake.position("deb-systemd-invoke stop vonk-forge-package-rollback.service")
            < fake.position("retire-rollback")
    );
}

#[test]
fn removal_never_fails_however_many_steps_do() {
    let mut fake = Fake::machine();
    fake.failing.push("/usr/");
    run(&Hook::PrermRemove, &mut fake).unwrap();
    // Every step was still attempted.
    assert!(fake.ran("disable-linger") && fake.ran("disable vonk-forge-package-rollback"));
}

#[test]
fn a_failing_boot_recovery_enable_fails_the_configure_after_the_agent_is_enabled() {
    let mut fake = Fake::machine();
    fake.failing.push("is-enabled");
    fake.failing.push("enable vonk-forge-package-rollback");
    assert!(configure(None, &mut fake).is_err());
    assert!(fake.ran("enable vonk-forge-agent.service"));
}

#[test]
fn configure_reapplies_the_firewall_before_the_finisher() {
    let mut fake = Fake::machine();
    fake.put(
        "/etc/vonk-forge-agent/docker-firewall.conf",
        "",
        node(Kind::File, 0, 0, 0o600),
    );
    configure(Some("1.0"), &mut fake).unwrap();
    let reapply = fake.position("try-reload-or-restart vonk-forge-docker-firewall.service");
    assert!(fake.position("daemon-reload") < reapply);
    assert!(reapply < fake.position("systemd-run"));
}

#[test]
fn a_failing_firewall_reapply_never_fails_the_configure_and_no_config_means_no_reapply() {
    let mut fake = Fake::machine();
    fake.put(
        "/etc/vonk-forge-agent/docker-firewall.conf",
        "",
        node(Kind::File, 0, 0, 0o600),
    );
    fake.failing.push("try-reload-or-restart");
    configure(Some("1.0"), &mut fake).unwrap();
    assert!(fake.ran("systemd-run"));

    let mut fake = Fake::machine();
    configure(Some("1.0"), &mut fake).unwrap();
    assert!(!fake.ran("try-reload-or-restart"));
}
