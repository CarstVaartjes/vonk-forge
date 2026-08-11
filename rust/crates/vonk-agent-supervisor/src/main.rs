#![forbid(unsafe_code)]

use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use clap::{Parser, Subcommand, ValueEnum};
use vonk_agent_supervisor::health::ReadinessEvidence;
use vonk_agent_supervisor::slots::{
    NoCrash, Slot, SlotPaths, SlotStore, SupervisorStatus, Transition,
};
use wait_timeout::ChildExt;

const DATA_ROOT: &str = "/var/lib/vonk-forge";
const RUNTIME_ROOT: &str = "/run/vonk-forge-agent";
const RELEASE_KEY: &str = "/usr/share/keyrings/vonk-forge-release.pub";
const AGENT_USER: &str = "vonk-agent";

#[derive(Parser)]
#[command(
    name = "vonk-agent-supervisor",
    version,
    about = "Stable Vonk Forge A/B agent supervisor"
)]
struct Cli {
    #[command(subcommand)]
    command: SupervisorCommand,
}

#[derive(Subcommand)]
enum SupervisorCommand {
    Initialize {
        #[arg(long)]
        slot: CliSlot,
    },
    Activate {
        #[arg(long)]
        slot: CliSlot,
        #[arg(long)]
        sha256: String,
    },
    PackageStagingSlot,
    RunAgent,
    Supervise,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum CliSlot {
    A,
    B,
}

impl From<CliSlot> for Slot {
    fn from(value: CliSlot) -> Self {
        match value {
            CliSlot::A => Self::A,
            CliSlot::B => Self::B,
        }
    }
}

fn main() {
    if let Err(error) = run(Cli::parse()) {
        eprintln!("vonk-agent-supervisor: {error}");
        std::process::exit(1);
    }
}

fn run(cli: Cli) -> Result<(), String> {
    let paths = SlotPaths::under(Path::new(DATA_ROOT), Path::new(RUNTIME_ROOT));
    let release_key = load_root_public_key(Path::new(RELEASE_KEY))?;
    let agent_uid = user_uid(Path::new("/etc/passwd"), AGENT_USER)?;
    let store = SlotStore::new(paths.clone(), &release_key, Some(0)).map_err(display)?;
    match cli.command {
        SupervisorCommand::Initialize { slot } => {
            require_root()?;
            store
                .initialize(slot.into(), unix_time()?)
                .map_err(display)?;
        }
        SupervisorCommand::Activate { slot, sha256 } => {
            require_root()?;
            store
                .activate(slot.into(), &sha256, unix_time()?, &NoCrash)
                .map_err(display)?;
            remove_readiness(&paths)?;
            systemctl_required(&[
                "--no-block",
                "restart",
                "vonk-forge-agent-supervisor.service",
            ])?;
        }
        SupervisorCommand::PackageStagingSlot => {
            require_root()?;
            println!("{}", store.package_staging_slot().map_err(display)?.name());
        }
        SupervisorCommand::RunAgent => {
            if rustix::process::geteuid().as_raw() != agent_uid {
                return Err("run-agent must execute as the dedicated agent user".to_owned());
            }
            exec_agent(&store)?;
        }
        SupervisorCommand::Supervise => {
            require_root()?;
            supervise(&store, &paths, agent_uid)?;
        }
    }
    Ok(())
}

fn exec_agent(store: &SlotStore) -> Result<(), String> {
    let (executable, state) = store.verified_active_executable().map_err(display)?;
    let credentials = std::env::var("CREDENTIALS_DIRECTORY")
        .map_err(|_| "systemd credential directory is unavailable".to_owned())?;
    let credentials_path = Path::new(&credentials);
    if !credentials_path.is_absolute()
        || !credentials_path.starts_with("/run/credentials/")
        || credentials_path
            .components()
            .any(|component| matches!(component, std::path::Component::ParentDir))
    {
        return Err("systemd credential directory is invalid".to_owned());
    }
    let error = Command::new(executable)
        .arg("--config")
        .arg("/etc/vonk-forge-agent/agent.toml")
        .arg("run")
        .env_clear()
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .env("PATH", "/usr/bin:/bin")
        .env("HOME", "/var/lib/vonk-forge-agent")
        .env("XDG_DATA_HOME", "/var/lib/vonk-forge-agent")
        .env("XDG_RUNTIME_DIR", "/run/vonk-forge-agent")
        .env(
            "CONTAINERS_STORAGE_CONF",
            "/etc/vonk-forge-agent/containers-storage.conf",
        )
        .env("CREDENTIALS_DIRECTORY", credentials)
        .env("VONK_SUPERVISOR_GENERATION", state.generation.to_string())
        .env("VONK_SUPERVISOR_SLOT", state.active_slot.name())
        .env("VONK_SUPERVISOR_SHA256", state.active_artifact_sha256)
        .env(
            "VONK_SUPERVISOR_STATE_SCHEMA",
            state.state_schema.to_string(),
        )
        .exec();
    Err(format!("verified agent execution failed: {error}"))
}

fn supervise(store: &SlotStore, paths: &SlotPaths, agent_uid: u32) -> Result<(), String> {
    let recovery = store.recover(unix_time()?).map_err(display)?;
    let state = store.load().map_err(display)?;
    let challenge = store.challenge().map_err(display)?;
    if vonk_agent_protocol::hex_sha256(challenge.as_bytes()) != state.challenge_sha256 {
        return Err("activation challenge does not match durable state".to_owned());
    }
    if recovery == Transition::RecoveryRequired {
        let _ = systemctl(&["stop", "vonk-forge-agent.service"]);
        return Err("supervisor requires explicit recovery".to_owned());
    }
    remove_readiness(paths)?;
    systemctl_required(&["restart", "vonk-forge-agent.service"])?;
    if state.status == SupervisorStatus::Stable {
        return Ok(());
    }

    loop {
        thread::sleep(Duration::from_secs(1));
        if let Ok(readiness) = ReadinessEvidence::load(&paths.readiness, Some(agent_uid)) {
            match store
                .observe_readiness(&readiness, unix_time()?)
                .map_err(display)?
            {
                Transition::Stable => return Ok(()),
                Transition::RollbackStarted => restart_after_transition(paths)?,
                Transition::RecoveryRequired => {
                    let _ = systemctl(&["stop", "vonk-forge-agent.service"]);
                    return Err("rollback target did not become ready".to_owned());
                }
                Transition::Waiting | Transition::Restart => {}
            }
        }
        if !systemctl(&["is-active", "--quiet", "vonk-forge-agent.service"])? {
            match store.record_agent_failure(unix_time()?).map_err(display)? {
                Transition::Restart | Transition::RollbackStarted => {
                    restart_after_transition(paths)?
                }
                Transition::RecoveryRequired => {
                    let _ = systemctl(&["stop", "vonk-forge-agent.service"]);
                    return Err("agent crash loop requires explicit recovery".to_owned());
                }
                Transition::Stable | Transition::Waiting => {}
            }
        }
        match store.check_deadline(unix_time()?).map_err(display)? {
            Transition::RollbackStarted => restart_after_transition(paths)?,
            Transition::RecoveryRequired => {
                let _ = systemctl(&["stop", "vonk-forge-agent.service"]);
                return Err("activation deadline requires explicit recovery".to_owned());
            }
            Transition::Stable => return Ok(()),
            Transition::Waiting | Transition::Restart => {}
        }
    }
}

fn restart_after_transition(paths: &SlotPaths) -> Result<(), String> {
    remove_readiness(paths)?;
    systemctl_required(&["restart", "vonk-forge-agent.service"])
}

fn remove_readiness(paths: &SlotPaths) -> Result<(), String> {
    match fs::symlink_metadata(&paths.readiness) {
        Ok(metadata) if metadata.file_type().is_symlink() || metadata.is_file() => {
            fs::remove_file(&paths.readiness).map_err(display)
        }
        Ok(_) => Err("readiness path is unsafe".to_owned()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(display(error)),
    }
}

fn systemctl_required(arguments: &[&str]) -> Result<(), String> {
    if systemctl(arguments)? {
        Ok(())
    } else {
        Err("systemctl rejected the compiled unit operation".to_owned())
    }
}

fn systemctl(arguments: &[&str]) -> Result<bool, String> {
    let mut command = Command::new("/usr/bin/systemctl");
    command
        .args(arguments)
        .env_clear()
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .env("PATH", "/usr/bin:/bin")
        .current_dir("/")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(Stdio::null());
    let mut child = command
        .spawn()
        .map_err(|_| "systemctl could not start".to_owned())?;
    let status = match child.wait_timeout(Duration::from_secs(15)) {
        Ok(Some(status)) => status,
        Ok(None) | Err(_) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err("systemctl exceeded its deadline".to_owned());
        }
    };
    Ok(status.success())
}

fn require_root() -> Result<(), String> {
    if rustix::process::geteuid().is_root() {
        Ok(())
    } else {
        Err("supervisor mutation requires root".to_owned())
    }
}

fn unix_time() -> Result<i64, String> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(display)?
        .as_secs() as i64)
}

fn load_root_public_key(path: &Path) -> Result<[u8; 32], String> {
    let value = read_root_text(path, 128)?;
    hex::decode(value)
        .map_err(display)?
        .try_into()
        .map_err(|_| "release public key must contain 32 bytes".to_owned())
}

fn user_uid(path: &Path, user: &str) -> Result<u32, String> {
    let document = read_root_text(path, 1024 * 1024)?;
    for line in document.lines() {
        let fields: Vec<_> = line.split(':').collect();
        if fields.len() == 7 && fields[0] == user {
            return fields[2].parse().map_err(display);
        }
    }
    Err(format!("required user {user} does not exist"))
}

fn read_root_text(path: &Path, maximum_bytes: u64) -> Result<String, String> {
    let metadata = fs::symlink_metadata(path).map_err(display)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.uid() != 0
        || metadata.permissions().mode() & 0o022 != 0
        || metadata.len() == 0
        || metadata.len() > maximum_bytes
    {
        return Err(format!("{} is unsafe", path.display()));
    }
    fs::read_to_string(path)
        .map(|value| value.trim_end().to_owned())
        .map_err(display)
}

fn display(error: impl std::fmt::Display) -> String {
    error.to_string()
}
