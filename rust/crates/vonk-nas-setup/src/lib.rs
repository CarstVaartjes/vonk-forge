#![forbid(unsafe_code)]

use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, Write};
use std::net::{IpAddr, Ipv4Addr};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use base64ct::{Base64, Base64UrlUnpadded, Encoding};
use ed25519_dalek::pkcs8::{DecodePrivateKey, EncodePrivateKey};
use p256::elliptic_curve::sec1::ToSec1Point;
use pkcs8::LineEnding;
use rcgen::{
    BasicConstraints, CertificateParams, CertifiedIssuer, DnType, ExtendedKeyUsagePurpose, IsCa,
    Issuer, KeyPair, KeyUsagePurpose, PKCS_ED25519,
};
use ring::rand::{SecureRandom, SystemRandom};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;

static STAGING_SEQUENCE: AtomicU64 = AtomicU64::new(0);
const CONTROLLER_CERTIFICATE_VALIDITY_DAYS: i64 = 397;
const CONTROLLER_CERTIFICATE_RENEWAL_THRESHOLD_DAYS: i64 = 30;

mod environment;
mod errors;
mod filesystem;
mod install;
mod pki;
mod pki_validation;
mod prompts;
mod secrets;
mod template;
mod upgrade;

use environment::{
    compose_profile_enabled, environment_value, parse_environment, read_existing_secret,
    render_owned_environment, required_value_default, retain_known_environment, secret_file_exists,
    set_environment_value, with_compose_profile,
};
pub use errors::{SetupError, root_rerun_hint};
use filesystem::{
    apply_secret_group, atomic_replace, atomic_replace_controller_leaf, create_secure_directory,
    create_staging_directory, ensure_safe_output_root, ensure_secure_directory,
    remove_retired_runtime_configs, sync_directory, validate_existing_bundle, write_new_file,
    write_secret_file,
};
use install::{
    BUNDLE_DIRECTORIES, GATEWAY_SECRET_DIRECTORY, at_path, collect_required_values,
    generate_missing_secrets, secret_file_content,
};
pub use install::{SetupMode, SetupOutcome, SetupRequest, prepare};
use pki::{
    PrivateJwk, PublicJwk, STEP_CA_CRT, STEP_CA_KEY, STEP_CA_ROOT, StepCaConfigIdentity,
    controller_certificate_params, generate_pki, pki_hostnames,
};
use pki_validation::{invalid_pki, validate_pki_material, validate_upgrade_pki_material};
pub use prompts::{EchoedSecretInput, HiddenSecretInput, PromptIo, SecretInput};
use prompts::{
    canonical_ed25519_pkcs8_pem, generate_ed25519_key, random_bytes, validate_ed25519_private_key,
    validate_single_line_secret,
};
pub use secrets::{OsSecretGenerator, SecretGenerationError, SecretGenerator};
pub use template::parse_template_payload;
use template::{
    generated_secrets, is_safe_secret_component, step_ca_files, valid_hostname,
    valid_required_value, validate_env_name,
};
use upgrade::{ControllerLeafReplacement, upgrade};

pub use vonk_agent_protocol::generated::NasInstallTemplate as CanonicalTemplatePayload;
use vonk_agent_protocol::generated::{
    NasGeneratedSecrets as GeneratedSecrets, NasHermesPrompt as HermesPrompt,
    NasInstallModes as InstallModes, NasPostgresUrlRequest as PostgresUrlRequest,
    NasRequiredValuePrompt as RequiredValuePrompt,
    NasRequiredValuePromptValidation as RequiredValueValidation, NasSecretPrompt as SecretPrompt,
    NasStepCaControllerFiles as StepCaControllerFiles,
    NasStepCaControllerRequest as StepCaControllerRequest,
};
