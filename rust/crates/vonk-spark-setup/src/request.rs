//! Request.

use super::*;
use vonk_agent_protocol::generated::InstallerReleaseManifest;

#[derive(Debug, Clone)]
pub struct SetupRequest {
    pub(super) package: PathBuf,
    pub(super) release_manifest: PathBuf,
    pub(super) release_signature: PathBuf,
    pub(super) setup_signature: PathBuf,
    pub(super) executable: PathBuf,
    pub(super) controller_address: Option<Ipv4Addr>,
    pub(super) enrollment_url: Option<Url>,
    pub(super) ca_sha256: Option<String>,
    pub(super) enroll: bool,
    pub(super) firewall_inputs: FirewallInputs,
}

#[derive(Debug, Clone, Default)]
pub struct FirewallInputs {
    pub(super) nas_management_ip: Option<String>,
    pub(super) node_management_ip: Option<String>,
    pub(super) node_fabric_ip: Option<String>,
    pub(super) peer_fabric_ip: Option<String>,
}

impl FirewallInputs {
    pub fn from_environment() -> Result<Self, SetupError> {
        fn optional(name: &'static str) -> Result<Option<String>, SetupError> {
            match std::env::var(name) {
                Ok(value) => Ok(Some(value)),
                Err(std::env::VarError::NotPresent) => Ok(None),
                Err(std::env::VarError::NotUnicode(_)) => Err(SetupError::UnsafeInput(name)),
            }
        }

        Ok(Self {
            nas_management_ip: optional("VONK_NAS_MANAGEMENT_IP")?,
            node_management_ip: optional("VONK_NODE_MANAGEMENT_IP")?,
            node_fabric_ip: optional("VONK_NODE_FABRIC_IP")?,
            peer_fabric_ip: optional("VONK_PEER_FABRIC_IP")?,
        })
    }
}

#[derive(Debug, Clone)]
pub struct ReleaseAuthority {
    pub(super) public_key_pem: Vec<u8>,
}

impl ReleaseAuthority {
    pub fn canonical() -> Self {
        Self {
            public_key_pem: INSTALLER_RELEASE_PUBLIC_KEY.to_vec(),
        }
    }

    pub fn from_pem(public_key_pem: Vec<u8>) -> Result<Self, SetupError> {
        if public_key_pem.is_empty()
            || public_key_pem.len() > 16 * 1024
            || !public_key_pem.starts_with(b"-----BEGIN PUBLIC KEY-----\n")
            || !public_key_pem.ends_with(b"-----END PUBLIC KEY-----\n")
        {
            return Err(SetupError::ReleaseSignature);
        }
        Ok(Self { public_key_pem })
    }

    pub(super) fn verify(
        &self,
        manifest: &[u8],
        encoded_signature: &[u8],
    ) -> Result<(), SetupError> {
        self.verify_bounded(manifest, encoded_signature, MAX_RELEASE_BYTES)
    }

    /// Authenticate the exact publication bytes before parsing the complete canonical graph.
    pub fn verify_manifest(
        &self,
        manifest: &[u8],
        encoded_signature: &[u8],
    ) -> Result<InstallerReleaseManifest, SetupError> {
        self.verify(manifest, encoded_signature)?;
        serde_json::from_slice(manifest).map_err(|_| SetupError::ReleaseSignature)
    }

    pub(super) fn verify_setup(
        &self,
        setup: &[u8],
        encoded_signature: &[u8],
    ) -> Result<(), SetupError> {
        self.verify_bounded(setup, encoded_signature, 64 * 1024 * 1024)
    }

    pub(super) fn verify_bounded(
        &self,
        payload: &[u8],
        encoded_signature: &[u8],
        maximum_payload: usize,
    ) -> Result<(), SetupError> {
        if payload.is_empty()
            || payload.len() > maximum_payload
            || encoded_signature.is_empty()
            || encoded_signature.len() > MAX_RELEASE_SIGNATURE_BYTES
            || !encoded_signature.ends_with(b"\n")
            || encoded_signature[..encoded_signature.len() - 1].contains(&b'\n')
        {
            return Err(SetupError::ReleaseSignature);
        }
        let directory = secure_tempdir("vonk-spark-release.")?;
        let key = directory.path().join("release-public.pem");
        let claims = directory.path().join("signed-payload");
        let encoded_signature_path = directory.path().join("release.sig.b64");
        let signature_path = directory.path().join("release.sig");
        fs::write(&key, &self.public_key_pem).map_err(SetupError::PrivilegedWrite)?;
        fs::write(&claims, payload).map_err(SetupError::PrivilegedWrite)?;
        fs::write(&encoded_signature_path, encoded_signature)
            .map_err(SetupError::PrivilegedWrite)?;
        let decoded = ProcessCommand::new("/usr/bin/openssl")
            .args(["base64", "-d", "-A", "-in"])
            .arg(&encoded_signature_path)
            .args(["-out"])
            .arg(&signature_path)
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map_err(|_| SetupError::ReleaseSignature)?;
        let decoded_size = fs::metadata(&signature_path)
            .map_err(|_| SetupError::ReleaseSignature)?
            .len();
        if !decoded.success() || decoded_size == 0 || decoded_size > 1024 {
            return Err(SetupError::ReleaseSignature);
        }
        let status = ProcessCommand::new("/usr/bin/openssl")
            .args(["dgst", "-sha256", "-verify"])
            .arg(&key)
            .args(["-signature"])
            .arg(&signature_path)
            .arg(&claims)
            .env_clear()
            .env("LANG", "C.UTF-8")
            .env("LC_ALL", "C.UTF-8")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map_err(|_| SetupError::ReleaseSignature)?;
        if status.success() {
            Ok(())
        } else {
            Err(SetupError::ReleaseSignature)
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CallerIdentity {
    pub(super) effective_uid: u32,
    pub(super) sudo_uid: Option<u32>,
}

impl CallerIdentity {
    pub fn unprivileged(uid: u32) -> Self {
        Self {
            effective_uid: uid,
            sudo_uid: None,
        }
    }

    pub fn sudo_root(uid: u32) -> Self {
        Self {
            effective_uid: 0,
            sudo_uid: Some(uid),
        }
    }

    pub fn direct_root() -> Self {
        Self {
            effective_uid: 0,
            sudo_uid: None,
        }
    }

    pub fn current() -> Result<Self, SetupError> {
        let effective_uid = rustix::process::geteuid().as_raw();
        let sudo_uid = std::env::var("SUDO_UID")
            .ok()
            .map(|value| value.parse::<u32>())
            .transpose()
            .map_err(|_| SetupError::CallerPhase)?;
        Ok(Self {
            effective_uid,
            sudo_uid,
        })
    }

    pub fn ensure_public(self) -> Result<(), SetupError> {
        self.require_unprivileged().map(|_| ())
    }

    pub(super) fn require_unprivileged(self) -> Result<u32, SetupError> {
        if self.effective_uid == 0 || self.sudo_uid.is_some() {
            return Err(SetupError::CallerPhase);
        }
        Ok(self.effective_uid)
    }

    pub(super) fn require_sudo_root(self, expected_uid: u32) -> Result<(), SetupError> {
        if self.effective_uid != 0 || expected_uid == 0 || self.sudo_uid != Some(expected_uid) {
            return Err(SetupError::CallerPhase);
        }
        Ok(())
    }

    pub(super) fn authenticate_for(self, paths: &InstallPaths) -> Result<(), SetupError> {
        if paths.required_owner.is_some() && self != Self::current()? {
            return Err(SetupError::CallerPhase);
        }
        Ok(())
    }
}
