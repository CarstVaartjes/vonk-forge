use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use ring::rand::{SecureRandom, SystemRandom};
use ring::signature::{self, Ed25519KeyPair, KeyPair};
use rustix::fs::{FlockOperation, flock};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use vonk_agent_protocol::{canonical_json, hex_sha256};

use crate::health::ReadinessEvidence;

const MANIFEST_DOMAIN: &[u8] = b"VONK-AGENT-SLOT-MANIFEST-V1\0";
const MAX_ARTIFACT_BYTES: u64 = 512 * 1024 * 1024;
const MAX_STATE_BYTES: u64 = 16 * 1024;
const ACTIVATION_WINDOW_SECONDS: i64 = 120;
const MAX_BOOT_ATTEMPTS: u8 = 3;
static PUBLICATION_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Error)]
pub enum SupervisorError {
    #[error("slot metadata is invalid")]
    InvalidManifest,
    #[error("slot artifact is invalid")]
    InvalidArtifact,
    #[error("supervisor state is invalid")]
    InvalidState,
    #[error("supervisor operation is not permitted")]
    InvalidTransition,
    #[error("supervisor managed path is unsafe")]
    UnsafePath,
    #[error("supervisor crash point was triggered: {0}")]
    Crash(String),
    #[error("supervisor I/O failed")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Slot {
    A,
    B,
}

impl Slot {
    pub fn name(self) -> &'static str {
        match self {
            Self::A => "a",
            Self::B => "b",
        }
    }

    fn other(self) -> Self {
        match self {
            Self::A => Self::B,
            Self::B => Self::A,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SlotManifestClaims {
    pub schema_version: u8,
    pub slot: Slot,
    pub architecture: String,
    pub artifact_sha256: String,
    pub artifact_size: u64,
    pub writes_state_schema: u32,
    pub min_readable_state_schema: u32,
    pub max_readable_state_schema: u32,
}

impl SlotManifestClaims {
    fn validate(&self) -> Result<(), SupervisorError> {
        if self.schema_version != 1
            || self.architecture != "aarch64"
            || !lower_hex(&self.artifact_sha256, 64)
            || !(64..=MAX_ARTIFACT_BYTES).contains(&self.artifact_size)
            || self.writes_state_schema == 0
            || self.min_readable_state_schema == 0
            || self.min_readable_state_schema > self.writes_state_schema
            || self.writes_state_schema > self.max_readable_state_schema
        {
            return Err(SupervisorError::InvalidManifest);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ManifestSignature {
    pub algorithm: String,
    pub key_id: String,
    pub value: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SlotManifest {
    pub schema_version: u8,
    pub claims: SlotManifestClaims,
    pub signature: ManifestSignature,
}

impl SlotManifest {
    pub fn signed(
        claims: SlotManifestClaims,
        signer: &Ed25519KeyPair,
    ) -> Result<Self, SupervisorError> {
        claims.validate()?;
        Ok(Self {
            schema_version: 1,
            signature: ManifestSignature {
                algorithm: "ed25519".to_owned(),
                key_id: hex_sha256(signer.public_key().as_ref()),
                value: hex::encode(signer.sign(&manifest_signing_bytes(&claims)?).as_ref()),
            },
            claims,
        })
    }
}

pub fn manifest_signing_bytes(claims: &SlotManifestClaims) -> Result<Vec<u8>, SupervisorError> {
    claims.validate()?;
    let mut value = MANIFEST_DOMAIN.to_vec();
    value.extend(canonical_json(claims).map_err(|_| SupervisorError::InvalidManifest)?);
    Ok(value)
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum SupervisorStatus {
    Stable,
    Pending,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SupervisorState {
    pub schema_version: u8,
    pub generation: u64,
    pub status: SupervisorStatus,
    pub active_slot: Slot,
    pub previous_slot: Option<Slot>,
    pub active_artifact_sha256: String,
    pub previous_artifact_sha256: Option<String>,
    pub state_schema: u32,
    pub activation_deadline: Option<i64>,
    pub boot_attempts: u8,
    pub rollback_performed: bool,
    pub challenge_sha256: String,
    pub reason: Option<String>,
}

impl SupervisorState {
    fn validate(&self) -> Result<(), SupervisorError> {
        if self.schema_version != 1
            || self.generation == 0
            || self.state_schema == 0
            || !lower_hex(&self.active_artifact_sha256, 64)
            || !lower_hex(&self.challenge_sha256, 64)
            || self
                .previous_artifact_sha256
                .as_deref()
                .is_some_and(|value| !lower_hex(value, 64))
            || self.previous_slot.is_some() != self.previous_artifact_sha256.is_some()
            || self.previous_slot == Some(self.active_slot)
            || self.boot_attempts > MAX_BOOT_ATTEMPTS
            || match self.status {
                SupervisorStatus::Stable => {
                    self.activation_deadline.is_some() || self.boot_attempts != 0
                }
                SupervisorStatus::Pending => self
                    .activation_deadline
                    .is_none_or(|deadline| deadline <= 0),
                SupervisorStatus::Failed => self.activation_deadline.is_some(),
            }
        {
            return Err(SupervisorError::InvalidState);
        }
        Ok(())
    }
}

#[derive(Debug, Clone)]
pub struct SlotPaths {
    pub data: PathBuf,
    pub slots: PathBuf,
    pub supervisor: PathBuf,
    pub runtime: PathBuf,
    pub current: PathBuf,
    pub previous: PathBuf,
    pub state: PathBuf,
    pub lock: PathBuf,
    pub challenge: PathBuf,
    pub readiness: PathBuf,
    pub rollback_reason: PathBuf,
}

impl SlotPaths {
    pub fn under(data: &Path, runtime: &Path) -> Self {
        let supervisor = data.join("supervisor");
        Self {
            data: data.to_path_buf(),
            slots: data.join("slots"),
            supervisor: supervisor.clone(),
            runtime: runtime.to_path_buf(),
            current: supervisor.join("current"),
            previous: supervisor.join("previous"),
            state: supervisor.join("state.json"),
            lock: supervisor.join("supervisor.lock"),
            challenge: supervisor.join("activation-challenge"),
            readiness: runtime.join("readiness.json"),
            rollback_reason: supervisor.join("rollback-reason.json"),
        }
    }

    pub fn slot_dir(&self, slot: Slot) -> PathBuf {
        self.slots.join(slot.name())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CrashPoint {
    AfterState,
    AfterPreviousPointer,
    AfterCurrentPointer,
}

pub trait CrashHook {
    fn hit(&self, point: CrashPoint) -> Result<(), String>;
}

pub struct NoCrash;

impl CrashHook for NoCrash {
    fn hit(&self, _point: CrashPoint) -> Result<(), String> {
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Transition {
    Stable,
    Waiting,
    Restart,
    RollbackStarted,
    RecoveryRequired,
}

pub struct SlotStore {
    paths: SlotPaths,
    release_public_key: [u8; 32],
    release_key_id: String,
    required_owner_uid: Option<u32>,
}

impl SlotStore {
    pub fn new(
        paths: SlotPaths,
        release_public_key: &[u8],
        required_owner_uid: Option<u32>,
    ) -> Result<Self, SupervisorError> {
        let release_public_key: [u8; 32] = release_public_key
            .try_into()
            .map_err(|_| SupervisorError::InvalidManifest)?;
        if !paths.data.is_absolute()
            || !paths.runtime.is_absolute()
            || ![
                &paths.slots,
                &paths.supervisor,
                &paths.current,
                &paths.previous,
                &paths.state,
                &paths.lock,
                &paths.challenge,
                &paths.rollback_reason,
            ]
            .iter()
            .all(|path| path.starts_with(&paths.data))
            || !paths.readiness.starts_with(&paths.runtime)
        {
            return Err(SupervisorError::UnsafePath);
        }
        Ok(Self {
            paths,
            release_key_id: hex_sha256(&release_public_key),
            release_public_key,
            required_owner_uid,
        })
    }

    pub fn initialize(&self, slot: Slot, _now: i64) -> Result<SupervisorState, SupervisorError> {
        self.with_lock(|| {
            if self.paths.state.exists() {
                return Err(SupervisorError::InvalidTransition);
            }
            self.require_roots()?;
            let manifest = self.verify_slot(slot, None)?;
            let challenge = self.publish_challenge()?;
            let state = SupervisorState {
                schema_version: 1,
                generation: 1,
                status: SupervisorStatus::Stable,
                active_slot: slot,
                previous_slot: None,
                active_artifact_sha256: manifest.claims.artifact_sha256,
                previous_artifact_sha256: None,
                state_schema: manifest.claims.writes_state_schema,
                activation_deadline: None,
                boot_attempts: 0,
                rollback_performed: false,
                challenge_sha256: hex_sha256(challenge.as_bytes()),
                reason: None,
            };
            self.publish_state(&state)?;
            self.publish_pointer(&self.paths.current, slot)?;
            self.remove_pointer(&self.paths.previous)?;
            Ok(state)
        })
    }

    pub fn activate(
        &self,
        slot: Slot,
        expected_digest: &str,
        now: i64,
        crash: &dyn CrashHook,
    ) -> Result<SupervisorState, SupervisorError> {
        self.with_lock(|| {
            let current = self.load_unlocked()?;
            if current.status != SupervisorStatus::Stable
                || slot == current.active_slot
                || now <= 0
                || !lower_hex(expected_digest, 64)
            {
                return Err(SupervisorError::InvalidTransition);
            }
            let previous =
                self.verify_slot(current.active_slot, Some(&current.active_artifact_sha256))?;
            let candidate = self.verify_slot(slot, Some(expected_digest))?;
            if !(candidate.claims.min_readable_state_schema
                ..=candidate.claims.max_readable_state_schema)
                .contains(&current.state_schema)
                || !(previous.claims.min_readable_state_schema
                    ..=previous.claims.max_readable_state_schema)
                    .contains(&candidate.claims.writes_state_schema)
            {
                return Err(SupervisorError::InvalidTransition);
            }
            let challenge = self.publish_challenge()?;
            let state = SupervisorState {
                schema_version: 1,
                generation: current.generation + 1,
                status: SupervisorStatus::Pending,
                active_slot: slot,
                previous_slot: Some(current.active_slot),
                active_artifact_sha256: candidate.claims.artifact_sha256,
                previous_artifact_sha256: Some(previous.claims.artifact_sha256),
                state_schema: candidate.claims.writes_state_schema,
                activation_deadline: Some(now + ACTIVATION_WINDOW_SECONDS),
                boot_attempts: 0,
                rollback_performed: false,
                challenge_sha256: hex_sha256(challenge.as_bytes()),
                reason: None,
            };
            self.publish_state(&state)?;
            crash
                .hit(CrashPoint::AfterState)
                .map_err(SupervisorError::Crash)?;
            self.publish_pointer(&self.paths.previous, current.active_slot)?;
            crash
                .hit(CrashPoint::AfterPreviousPointer)
                .map_err(SupervisorError::Crash)?;
            self.publish_pointer(&self.paths.current, slot)?;
            crash
                .hit(CrashPoint::AfterCurrentPointer)
                .map_err(SupervisorError::Crash)?;
            Ok(state)
        })
    }

    pub fn load(&self) -> Result<SupervisorState, SupervisorError> {
        self.require_roots()?;
        self.load_unlocked()
    }

    pub fn package_staging_slot(&self) -> Result<Slot, SupervisorError> {
        self.require_roots()?;
        let state = self.load_unlocked()?;
        if state.status != SupervisorStatus::Stable {
            return Err(SupervisorError::InvalidTransition);
        }
        self.verify_slot(state.active_slot, Some(&state.active_artifact_sha256))?;
        Ok(state.active_slot.other())
    }

    pub fn recover_pointers(&self) -> Result<(), SupervisorError> {
        self.with_lock(|| {
            let state = self.load_unlocked()?;
            self.verify_slot(state.active_slot, Some(&state.active_artifact_sha256))?;
            self.publish_pointer(&self.paths.current, state.active_slot)?;
            match state.previous_slot {
                Some(slot) => self.publish_pointer(&self.paths.previous, slot)?,
                None => self.remove_pointer(&self.paths.previous)?,
            }
            Ok(())
        })
    }

    pub fn recover(&self, now: i64) -> Result<Transition, SupervisorError> {
        self.with_lock(|| {
            let state = self.load_unlocked()?;
            if self
                .verify_slot(state.active_slot, Some(&state.active_artifact_sha256))
                .is_err()
            {
                if state.status == SupervisorStatus::Pending {
                    return self.rollback_or_fail(state, "active-slot-invalid", now);
                }
                return Err(SupervisorError::InvalidArtifact);
            }
            self.publish_pointer(&self.paths.current, state.active_slot)?;
            match state.previous_slot {
                Some(slot) => self.publish_pointer(&self.paths.previous, slot)?,
                None => self.remove_pointer(&self.paths.previous)?,
            }
            Ok(match state.status {
                SupervisorStatus::Stable => Transition::Stable,
                SupervisorStatus::Pending => Transition::Waiting,
                SupervisorStatus::Failed => Transition::RecoveryRequired,
            })
        })
    }

    pub fn observe_readiness(
        &self,
        readiness: &ReadinessEvidence,
        now: i64,
    ) -> Result<Transition, SupervisorError> {
        self.with_lock(|| {
            let mut state = self.load_unlocked()?;
            if state.status != SupervisorStatus::Pending {
                return Ok(if state.status == SupervisorStatus::Stable {
                    Transition::Stable
                } else {
                    Transition::RecoveryRequired
                });
            }
            if now > state.activation_deadline.unwrap_or(0) {
                return self.rollback_or_fail(state, "readiness-timeout", now);
            }
            readiness
                .validate()
                .map_err(|_| SupervisorError::InvalidState)?;
            let challenge_matches =
                hex_sha256(readiness.challenge.as_bytes()) == state.challenge_sha256;
            if readiness.generation != state.generation
                || readiness.slot != state.active_slot
                || readiness.artifact_sha256 != state.active_artifact_sha256
                || readiness.state_schema != state.state_schema
                || !challenge_matches
            {
                return Ok(Transition::Waiting);
            }
            self.verify_slot(state.active_slot, Some(&state.active_artifact_sha256))?;
            state.status = SupervisorStatus::Stable;
            state.activation_deadline = None;
            state.boot_attempts = 0;
            self.publish_state(&state)?;
            Ok(Transition::Stable)
        })
    }

    pub fn record_agent_failure(&self, now: i64) -> Result<Transition, SupervisorError> {
        self.with_lock(|| {
            let mut state = self.load_unlocked()?;
            if state.status != SupervisorStatus::Pending {
                return Err(SupervisorError::InvalidTransition);
            }
            if state.rollback_performed {
                state.boot_attempts += 1;
                if state.boot_attempts < MAX_BOOT_ATTEMPTS {
                    self.publish_state(&state)?;
                    return Ok(Transition::Restart);
                }
                state.status = SupervisorStatus::Failed;
                state.activation_deadline = None;
                state.reason = Some("rollback-target-failed".to_owned());
                self.publish_state(&state)?;
                self.publish_reason(&state, "rollback-target-failed", now)?;
                return Ok(Transition::RecoveryRequired);
            }
            state.boot_attempts += 1;
            if state.boot_attempts < MAX_BOOT_ATTEMPTS {
                self.publish_state(&state)?;
                Ok(Transition::Restart)
            } else {
                self.rollback_or_fail(state, "agent-crash-loop", now)
            }
        })
    }

    pub fn check_deadline(&self, now: i64) -> Result<Transition, SupervisorError> {
        self.with_lock(|| {
            let state = self.load_unlocked()?;
            if state.status != SupervisorStatus::Pending {
                return Ok(if state.status == SupervisorStatus::Stable {
                    Transition::Stable
                } else {
                    Transition::RecoveryRequired
                });
            }
            if now <= state.activation_deadline.unwrap_or(0) {
                Ok(Transition::Waiting)
            } else {
                self.rollback_or_fail(state, "readiness-timeout", now)
            }
        })
    }

    pub fn challenge(&self) -> Result<String, SupervisorError> {
        let metadata = fs::symlink_metadata(&self.paths.challenge)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.permissions().mode() & 0o777 != 0o600
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(SupervisorError::InvalidState);
        }
        let value = fs::read_to_string(&self.paths.challenge)?;
        let value = value.trim_end().to_owned();
        if !lower_hex(&value, 64) {
            return Err(SupervisorError::InvalidState);
        }
        Ok(value)
    }

    pub fn verified_active_executable(
        &self,
    ) -> Result<(PathBuf, SupervisorState), SupervisorError> {
        self.require_roots()?;
        let state = self.load_unlocked()?;
        self.verify_slot(state.active_slot, Some(&state.active_artifact_sha256))?;
        Ok((
            self.paths.slot_dir(state.active_slot).join("vonk-agent"),
            state,
        ))
    }

    fn rollback_or_fail(
        &self,
        mut state: SupervisorState,
        reason: &str,
        now: i64,
    ) -> Result<Transition, SupervisorError> {
        if state.rollback_performed {
            state.status = SupervisorStatus::Failed;
            state.activation_deadline = None;
            state.reason = Some(reason.to_owned());
            self.publish_state(&state)?;
            self.publish_reason(&state, reason, now)?;
            return Ok(Transition::RecoveryRequired);
        }
        let previous_slot = state
            .previous_slot
            .ok_or(SupervisorError::InvalidTransition)?;
        let previous_digest = state
            .previous_artifact_sha256
            .clone()
            .ok_or(SupervisorError::InvalidTransition)?;
        let previous = self.verify_slot(previous_slot, Some(&previous_digest))?;
        if !(previous.claims.min_readable_state_schema..=previous.claims.max_readable_state_schema)
            .contains(&state.state_schema)
        {
            return Err(SupervisorError::InvalidTransition);
        }
        let failed_slot = state.active_slot;
        let failed_digest = state.active_artifact_sha256.clone();
        let challenge = self.publish_challenge()?;
        state.generation += 1;
        state.active_slot = previous_slot;
        state.previous_slot = Some(failed_slot);
        state.active_artifact_sha256 = previous_digest;
        state.previous_artifact_sha256 = Some(failed_digest);
        state.state_schema = previous.claims.writes_state_schema;
        state.activation_deadline = Some(now + ACTIVATION_WINDOW_SECONDS);
        state.boot_attempts = 0;
        state.rollback_performed = true;
        state.challenge_sha256 = hex_sha256(challenge.as_bytes());
        state.reason = Some(reason.to_owned());
        self.publish_state(&state)?;
        self.publish_reason(&state, reason, now)?;
        self.publish_pointer(&self.paths.previous, failed_slot)?;
        self.publish_pointer(&self.paths.current, previous_slot)?;
        Ok(Transition::RollbackStarted)
    }

    fn verify_slot(
        &self,
        slot: Slot,
        expected_digest: Option<&str>,
    ) -> Result<SlotManifest, SupervisorError> {
        let directory = self.paths.slot_dir(slot);
        self.require_directory(&directory)?;
        let manifest_path = directory.join("manifest.json");
        let metadata = fs::symlink_metadata(&manifest_path)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.len() == 0
            || metadata.len() > 16 * 1024
            || metadata.permissions().mode() & 0o022 != 0
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(SupervisorError::InvalidManifest);
        }
        let raw = fs::read(&manifest_path)?;
        let manifest: SlotManifest =
            serde_json::from_slice(&raw).map_err(|_| SupervisorError::InvalidManifest)?;
        if canonical_json(&manifest).map_err(|_| SupervisorError::InvalidManifest)? != raw
            || manifest.schema_version != 1
            || manifest.claims.slot != slot
            || manifest.signature.algorithm != "ed25519"
            || manifest.signature.key_id != self.release_key_id
            || !lower_hex(&manifest.signature.value, 128)
        {
            return Err(SupervisorError::InvalidManifest);
        }
        manifest.claims.validate()?;
        let signature_bytes =
            hex::decode(&manifest.signature.value).map_err(|_| SupervisorError::InvalidManifest)?;
        signature::UnparsedPublicKey::new(&signature::ED25519, self.release_public_key)
            .verify(&manifest_signing_bytes(&manifest.claims)?, &signature_bytes)
            .map_err(|_| SupervisorError::InvalidManifest)?;
        if expected_digest.is_some_and(|value| value != manifest.claims.artifact_sha256) {
            return Err(SupervisorError::InvalidArtifact);
        }
        self.verify_artifact(&directory.join("vonk-agent"), &manifest.claims)?;
        Ok(manifest)
    }

    fn verify_artifact(
        &self,
        path: &Path,
        claims: &SlotManifestClaims,
    ) -> Result<(), SupervisorError> {
        let metadata = fs::symlink_metadata(path)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.len() != claims.artifact_size
            || metadata.permissions().mode() & 0o222 != 0
            || metadata.permissions().mode() & 0o111 == 0
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(SupervisorError::InvalidArtifact);
        }
        let mut file = File::open(path)?;
        let before = file.metadata()?;
        let mut hash = Sha256::new();
        let mut header = [0_u8; 64];
        file.read_exact(&mut header)
            .map_err(|_| SupervisorError::InvalidArtifact)?;
        hash.update(header);
        let mut buffer = [0_u8; 64 * 1024];
        let mut consumed = 64_u64;
        loop {
            let count = file.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            consumed += count as u64;
            if consumed > MAX_ARTIFACT_BYTES {
                return Err(SupervisorError::InvalidArtifact);
            }
            hash.update(&buffer[..count]);
        }
        let after = file.metadata()?;
        let machine = u16::from_le_bytes([header[18], header[19]]);
        let elf_type = u16::from_le_bytes([header[16], header[17]]);
        if stable_identity(&before) != stable_identity(&after)
            || header[..7] != *b"\x7fELF\x02\x01\x01"
            || !matches!(elf_type, 2 | 3)
            || machine != 183
            || hex::encode(hash.finalize()) != claims.artifact_sha256
        {
            return Err(SupervisorError::InvalidArtifact);
        }
        Ok(())
    }

    fn load_unlocked(&self) -> Result<SupervisorState, SupervisorError> {
        let metadata = fs::symlink_metadata(&self.paths.state)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.len() == 0
            || metadata.len() > MAX_STATE_BYTES
            || metadata.permissions().mode() & 0o777 != 0o644
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(SupervisorError::InvalidState);
        }
        let raw = fs::read(&self.paths.state)?;
        let state: SupervisorState =
            serde_json::from_slice(&raw).map_err(|_| SupervisorError::InvalidState)?;
        state.validate()?;
        if canonical_json(&state).map_err(|_| SupervisorError::InvalidState)? != raw {
            return Err(SupervisorError::InvalidState);
        }
        Ok(state)
    }

    fn publish_state(&self, state: &SupervisorState) -> Result<(), SupervisorError> {
        state.validate()?;
        self.atomic_write(
            &self.paths.state,
            &canonical_json(state).map_err(|_| SupervisorError::InvalidState)?,
            0o644,
        )
    }

    fn publish_challenge(&self) -> Result<String, SupervisorError> {
        let mut bytes = [0_u8; 32];
        SystemRandom::new()
            .fill(&mut bytes)
            .map_err(|_| SupervisorError::InvalidState)?;
        let challenge = hex::encode(bytes);
        self.atomic_write(
            &self.paths.challenge,
            format!("{challenge}\n").as_bytes(),
            0o600,
        )?;
        Ok(challenge)
    }

    fn publish_reason(
        &self,
        state: &SupervisorState,
        reason: &str,
        recorded_at: i64,
    ) -> Result<(), SupervisorError> {
        #[derive(Serialize)]
        #[serde(deny_unknown_fields)]
        struct Reason<'a> {
            schema_version: u8,
            generation: u64,
            active_slot: Slot,
            reason: &'a str,
            recorded_at: i64,
        }
        let document = Reason {
            schema_version: 1,
            generation: state.generation,
            active_slot: state.active_slot,
            reason,
            recorded_at,
        };
        self.atomic_write(
            &self.paths.rollback_reason,
            &canonical_json(&document).map_err(|_| SupervisorError::InvalidState)?,
            0o600,
        )
    }

    fn atomic_write(&self, path: &Path, raw: &[u8], mode: u32) -> Result<(), SupervisorError> {
        let parent = path.parent().ok_or(SupervisorError::UnsafePath)?;
        self.require_directory(parent)?;
        let sequence = PUBLICATION_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let name = path
            .file_name()
            .and_then(|value| value.to_str())
            .ok_or(SupervisorError::UnsafePath)?;
        let temporary = parent.join(format!(".{name}.{}.{sequence}.new", std::process::id()));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(mode)
            .open(&temporary)?;
        file.set_permissions(fs::Permissions::from_mode(mode))?;
        file.write_all(raw)?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        sync_directory(parent)
    }

    fn publish_pointer(&self, path: &Path, slot: Slot) -> Result<(), SupervisorError> {
        if let Ok(metadata) = fs::symlink_metadata(path)
            && !metadata.file_type().is_symlink()
        {
            return Err(SupervisorError::UnsafePath);
        }
        let parent = path.parent().ok_or(SupervisorError::UnsafePath)?;
        self.require_directory(parent)?;
        let sequence = PUBLICATION_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let temporary = parent.join(format!(
            ".{}.{}.{sequence}.new",
            path.file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("pointer"),
            std::process::id()
        ));
        symlink(format!("../slots/{}", slot.name()), &temporary)?;
        if let Err(error) = fs::rename(&temporary, path) {
            let _ = fs::remove_file(&temporary);
            return Err(SupervisorError::Io(error));
        }
        sync_directory(parent)
    }

    fn remove_pointer(&self, path: &Path) -> Result<(), SupervisorError> {
        match fs::symlink_metadata(path) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                fs::remove_file(path)?;
                sync_directory(path.parent().ok_or(SupervisorError::UnsafePath)?)
            }
            Ok(_) => Err(SupervisorError::UnsafePath),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(SupervisorError::Io(error)),
        }
    }

    fn require_roots(&self) -> Result<(), SupervisorError> {
        for path in [&self.paths.data, &self.paths.slots, &self.paths.supervisor] {
            self.require_directory(path)?;
        }
        Ok(())
    }

    fn require_directory(&self, path: &Path) -> Result<(), SupervisorError> {
        let metadata = fs::symlink_metadata(path).map_err(|_| SupervisorError::UnsafePath)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_dir()
            || metadata.permissions().mode() & 0o022 != 0
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(SupervisorError::UnsafePath);
        }
        Ok(())
    }

    fn with_lock<T>(
        &self,
        operation: impl FnOnce() -> Result<T, SupervisorError>,
    ) -> Result<T, SupervisorError> {
        self.require_roots()?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(&self.paths.lock)?;
        let metadata = lock.metadata()?;
        if !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.permissions().mode() & 0o777 != 0o600
            || self
                .required_owner_uid
                .is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(SupervisorError::UnsafePath);
        }
        flock(&lock, FlockOperation::LockExclusive).map_err(std::io::Error::from)?;
        operation()
    }
}

fn stable_identity(metadata: &fs::Metadata) -> (u64, u64, u64, i64, i64) {
    (
        metadata.dev(),
        metadata.ino(),
        metadata.len(),
        metadata.mtime(),
        metadata.ctime(),
    )
}

fn sync_directory(path: &Path) -> Result<(), SupervisorError> {
    OpenOptions::new().read(true).open(path)?.sync_all()?;
    Ok(())
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}
