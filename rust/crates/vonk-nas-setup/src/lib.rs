#![forbid(unsafe_code)]

use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, Write};
use std::net::{IpAddr, Ipv4Addr};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use base64ct::{Base64, Base64UrlUnpadded, Encoding};
use ed25519_dalek::pkcs8::{DecodePrivateKey, EncodePrivateKey};
use p256::elliptic_curve::sec1::ToEncodedPoint;
use pkcs8::LineEnding;
use rand_core::{OsRng, RngCore};
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

#[derive(Debug, Error)]
pub enum SetupError {
    #[error("template payload is invalid: {0}")]
    InvalidPayload(String),
    #[error("destination is unsafe: {0}")]
    UnsafeDestination(String),
    #[error("bundle already exists; use explicit upgrade mode")]
    AlreadyExists,
    #[error("bundle does not exist or is incomplete")]
    MissingBundle,
    #[error("input ended before setup was complete")]
    InputEnded,
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
    #[error(transparent)]
    SecretGeneration(#[from] SecretGenerationError),
    #[error("generated secret material is invalid: {0}")]
    InvalidSecretMaterial(String),
}

#[derive(Debug, Error)]
#[error("operating-system random number generation failed")]
pub struct SecretGenerationError;

pub trait SecretGenerator {
    fn generate(&self, bytes: usize) -> Result<String, SecretGenerationError>;
}

#[derive(Debug, Default, Clone, Copy)]
pub struct OsSecretGenerator;

impl SecretGenerator for OsSecretGenerator {
    fn generate(&self, bytes: usize) -> Result<String, SecretGenerationError> {
        if bytes == 0 {
            return Err(SecretGenerationError);
        }
        let mut random = vec![0_u8; bytes];
        SystemRandom::new()
            .fill(&mut random)
            .map_err(|_| SecretGenerationError)?;
        Ok(hex::encode(random))
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CanonicalTemplatePayload {
    schema_version: u8,
    docker_compose_yaml: String,
    #[serde(default)]
    internal_values: Vec<InternalValue>,
    #[serde(default)]
    required_values: Vec<RequiredValuePrompt>,
    #[serde(default)]
    secrets: Vec<SecretPrompt>,
    #[serde(default)]
    generated_secrets: GeneratedSecrets,
    #[serde(default)]
    runtime_files: Vec<RuntimeFile>,
    step_ca_controller: Option<StepCaControllerRequest>,
    hermes: Option<HermesPrompt>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct InternalValue {
    env: String,
    value: String,
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct GeneratedSecrets {
    #[serde(default)]
    random_text: Vec<RandomTextRequest>,
    #[serde(default)]
    ed25519_pkcs8_pem: Vec<Ed25519KeyRequest>,
    #[serde(default)]
    postgres_urls: Vec<PostgresUrlRequest>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RandomTextRequest {
    file: String,
    prompt: String,
    bytes: usize,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Ed25519KeyRequest {
    file: String,
    prompt: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PostgresUrlRequest {
    file: String,
    password_file: String,
    scheme: String,
    username: String,
    host: String,
    port: u16,
    database: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimeFile {
    file: String,
    content: String,
    mode: u32,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct StepCaControllerRequest {
    prompt: String,
    hostname_envs: Vec<String>,
    provisioner_name: String,
    kid_env: String,
    password_bytes: usize,
    files: StepCaControllerFiles,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct StepCaControllerFiles {
    root_certificate: String,
    intermediate_certificate: String,
    intermediate_private_key: String,
    controller_server_certificate: String,
    controller_server_private_key: String,
    provisioner_private_jwk: String,
    provisioner_public_jwk: String,
    ca_config: String,
    password: String,
}

impl StepCaControllerFiles {
    fn all(&self) -> [&str; 9] {
        [
            &self.root_certificate,
            &self.intermediate_certificate,
            &self.intermediate_private_key,
            &self.controller_server_certificate,
            &self.controller_server_private_key,
            &self.provisioner_private_jwk,
            &self.provisioner_public_jwk,
            &self.ca_config,
            &self.password,
        ]
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RequiredValuePrompt {
    env: String,
    prompt: String,
    default: Option<String>,
    #[serde(default)]
    validation: RequiredValueValidation,
}

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
enum RequiredValueValidation {
    #[default]
    NonEmpty,
    Ipv4,
    CidrList,
    OptionalCidrList,
    Jurisdiction,
    Hostname,
    HttpsOrigin,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SecretPrompt {
    file: String,
    prompt: String,
    generate_bytes: Option<usize>,
    prefix: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct HermesPrompt {
    env: String,
    prompt: String,
    enabled_value: String,
    disabled_value: String,
    #[serde(default)]
    required_values: Vec<RequiredValuePrompt>,
    #[serde(default)]
    secrets: Vec<SecretPrompt>,
}

impl CanonicalTemplatePayload {
    pub fn from_json(raw: &[u8]) -> Result<Self, SetupError> {
        let payload: Self = serde_json::from_slice(raw)
            .map_err(|error| SetupError::InvalidPayload(error.to_string()))?;
        payload.validate()?;
        Ok(payload)
    }

    fn validate(&self) -> Result<(), SetupError> {
        if self.schema_version != 2 {
            return Err(SetupError::InvalidPayload(
                "unsupported schema version".to_owned(),
            ));
        }
        if self.docker_compose_yaml.is_empty() || self.docker_compose_yaml.contains('\0') {
            return Err(SetupError::InvalidPayload(
                "docker compose payload is empty or malformed".to_owned(),
            ));
        }

        let mut environment = HashSet::new();
        let mut secrets = HashSet::new();
        for value in &self.internal_values {
            validate_env_name(&value.env)?;
            if value.value.contains('\0') {
                return Err(SetupError::InvalidPayload(format!(
                    "internal value for {} contains NUL",
                    value.env
                )));
            }
            if !environment.insert(value.env.as_str()) {
                return Err(SetupError::InvalidPayload(format!(
                    "duplicate environment key {}",
                    value.env
                )));
            }
        }
        validate_prompts(
            &self.required_values,
            &self.secrets,
            &mut environment,
            &mut secrets,
        )?;
        if let Some(hermes) = &self.hermes {
            validate_env_name(&hermes.env)?;
            if !environment.insert(hermes.env.as_str()) {
                return Err(SetupError::InvalidPayload(format!(
                    "duplicate environment key {}",
                    hermes.env
                )));
            }
            if hermes.prompt.trim().is_empty() {
                return Err(SetupError::InvalidPayload(
                    "Hermes prompt is empty".to_owned(),
                ));
            }
            if hermes.enabled_value == hermes.disabled_value
                || hermes.enabled_value.contains(['\0', '\r', '\n'])
                || hermes.disabled_value.contains(['\0', '\r', '\n'])
            {
                return Err(SetupError::InvalidPayload(
                    "Hermes enabled and disabled values must be distinct single-line values"
                        .to_owned(),
                ));
            }
            validate_prompts(
                &hermes.required_values,
                &hermes.secrets,
                &mut environment,
                &mut secrets,
            )?;
        }
        for request in &self.generated_secrets.random_text {
            validate_generated_file(&request.file, &mut secrets)?;
            validate_generation_prompt(&request.prompt, &request.file)?;
            if !(16..=128).contains(&request.bytes) {
                return Err(SetupError::InvalidPayload(format!(
                    "generation size for {} is outside 16..=128 bytes",
                    request.file
                )));
            }
        }
        for request in &self.generated_secrets.ed25519_pkcs8_pem {
            validate_generated_file(&request.file, &mut secrets)?;
            validate_generation_prompt(&request.prompt, &request.file)?;
        }
        for request in &self.generated_secrets.postgres_urls {
            validate_generated_file(&request.file, &mut secrets)?;
            validate_secret_name(&request.password_file)?;
            if !secrets.contains(request.password_file.as_str()) {
                return Err(SetupError::InvalidPayload(format!(
                    "PostgreSQL URL {} references undeclared password file {}",
                    request.file, request.password_file
                )));
            }
            validate_postgres_url_request(request)?;
        }
        for runtime_file in &self.runtime_files {
            if !runtime_file.file.starts_with("runtime-configs/")
                || runtime_file.content.contains('\0')
                || runtime_file.content.len() > 1024 * 1024
                || !matches!(runtime_file.mode, 0o644 | 0o755)
            {
                return Err(SetupError::InvalidPayload(format!(
                    "runtime file {} is invalid",
                    runtime_file.file
                )));
            }
            validate_generated_file(&runtime_file.file, &mut secrets)?;
        }
        if let Some(request) = &self.step_ca_controller {
            if request.prompt.trim().is_empty()
                || request.provisioner_name.trim().is_empty()
                || request.provisioner_name.contains(['\0', '\r', '\n'])
                || !(16..=128).contains(&request.password_bytes)
                || request.hostname_envs.is_empty()
            {
                return Err(SetupError::InvalidPayload(
                    "Step CA/controller request is incomplete".to_owned(),
                ));
            }
            let mut hostnames = HashSet::new();
            for name in &request.hostname_envs {
                validate_env_name(name)?;
                if !environment.contains(name.as_str()) || !hostnames.insert(name.as_str()) {
                    return Err(SetupError::InvalidPayload(format!(
                        "Step CA hostname environment key {name} is undeclared or repeated"
                    )));
                }
            }
            validate_env_name(&request.kid_env)?;
            if !environment.insert(request.kid_env.as_str()) {
                return Err(SetupError::InvalidPayload(format!(
                    "duplicate environment key {}",
                    request.kid_env
                )));
            }
            for file in request.files.all() {
                validate_generated_file(file, &mut secrets)?;
            }
        }
        Ok(())
    }
}

fn validate_generated_file<'a>(
    file: &'a str,
    secrets: &mut HashSet<&'a str>,
) -> Result<(), SetupError> {
    validate_secret_name(file)?;
    if !secrets.insert(file) {
        return Err(SetupError::InvalidPayload(format!(
            "duplicate secret file {file}"
        )));
    }
    Ok(())
}

fn validate_generation_prompt(prompt: &str, file: &str) -> Result<(), SetupError> {
    if prompt.trim().is_empty() {
        return Err(SetupError::InvalidPayload(format!(
            "prompt for {file} is empty"
        )));
    }
    Ok(())
}

fn validate_postgres_url_request(request: &PostgresUrlRequest) -> Result<(), SetupError> {
    let valid_scheme = request
        .scheme
        .chars()
        .enumerate()
        .all(|(index, character)| {
            if index == 0 {
                character.is_ascii_lowercase()
            } else {
                character.is_ascii_lowercase()
                    || character.is_ascii_digit()
                    || matches!(character, '+' | '-' | '.')
            }
        });
    if !valid_scheme
        || request.username.is_empty()
        || request.host.is_empty()
        || request.port == 0
        || request.database.is_empty()
        || [&request.username, &request.host, &request.database]
            .iter()
            .any(|value| value.contains(['\0', '\r', '\n']))
    {
        return Err(SetupError::InvalidPayload(format!(
            "PostgreSQL URL request for {} is invalid",
            request.file
        )));
    }
    Ok(())
}

fn validate_prompts<'a>(
    values: &'a [RequiredValuePrompt],
    secret_prompts: &'a [SecretPrompt],
    environment: &mut HashSet<&'a str>,
    secrets: &mut HashSet<&'a str>,
) -> Result<(), SetupError> {
    for value in values {
        validate_env_name(&value.env)?;
        if value.prompt.trim().is_empty() {
            return Err(SetupError::InvalidPayload(format!(
                "prompt for {} is empty",
                value.env
            )));
        }
        if value
            .default
            .as_deref()
            .is_some_and(|default| !valid_required_value(default, &value.validation))
        {
            return Err(SetupError::InvalidPayload(format!(
                "default for {} does not satisfy its validator",
                value.env
            )));
        }
        if !environment.insert(&value.env) {
            return Err(SetupError::InvalidPayload(format!(
                "duplicate environment key {}",
                value.env
            )));
        }
    }
    for secret in secret_prompts {
        validate_secret_name(&secret.file)?;
        if secret.prompt.trim().is_empty() {
            return Err(SetupError::InvalidPayload(format!(
                "prompt for {} is empty",
                secret.file
            )));
        }
        if secret
            .generate_bytes
            .is_some_and(|bytes| !(16..=128).contains(&bytes))
        {
            return Err(SetupError::InvalidPayload(format!(
                "generation size for {} is outside 16..=128 bytes",
                secret.file
            )));
        }
        if secret.prefix.as_deref().is_some_and(|prefix| {
            prefix.is_empty()
                || prefix.len() > 32
                || !prefix.chars().all(|character| {
                    character.is_ascii_alphanumeric() || "_.~-".contains(character)
                })
        }) {
            return Err(SetupError::InvalidPayload(format!(
                "generation prefix for {} is invalid",
                secret.file
            )));
        }
        if !secrets.insert(&secret.file) {
            return Err(SetupError::InvalidPayload(format!(
                "duplicate secret file {}",
                secret.file
            )));
        }
        if secrets.iter().any(|existing| {
            *existing != secret.file
                && (existing
                    .strip_prefix(&secret.file)
                    .is_some_and(|suffix| suffix.starts_with('/'))
                    || secret
                        .file
                        .strip_prefix(*existing)
                        .is_some_and(|suffix| suffix.starts_with('/')))
        }) {
            return Err(SetupError::InvalidPayload(format!(
                "secret path {} conflicts with another secret",
                secret.file
            )));
        }
    }
    Ok(())
}

fn validate_env_name(name: &str) -> Result<(), SetupError> {
    let mut characters = name.chars();
    let valid = characters
        .next()
        .is_some_and(|character| character.is_ascii_uppercase() || character == '_')
        && characters.all(|character| {
            character.is_ascii_uppercase() || character.is_ascii_digit() || character == '_'
        });
    if !valid {
        return Err(SetupError::InvalidPayload(format!(
            "invalid environment key {name}"
        )));
    }
    Ok(())
}

fn valid_required_value(value: &str, validation: &RequiredValueValidation) -> bool {
    if value.contains(['\0', '\r', '\n']) {
        return false;
    }
    match validation {
        RequiredValueValidation::NonEmpty => !value.is_empty(),
        RequiredValueValidation::Ipv4 => value.parse::<Ipv4Addr>().is_ok(),
        RequiredValueValidation::CidrList => valid_cidr_list(value, false),
        RequiredValueValidation::OptionalCidrList => valid_cidr_list(value, true),
        RequiredValueValidation::Jurisdiction => {
            const ISO_ALPHA2: &str = " AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW EU ";
            value.len() == 2 && ISO_ALPHA2.contains(&format!(" {value} "))
        }
        RequiredValueValidation::Hostname => valid_hostname(value),
        RequiredValueValidation::HttpsOrigin => valid_https_origin(value),
    }
}

fn valid_cidr_list(value: &str, allow_empty: bool) -> bool {
    if value.is_empty() {
        return allow_empty;
    }
    value.split(',').all(|item| {
        let item = item.trim();
        let Some((address, prefix)) = item.split_once('/') else {
            return false;
        };
        if address.is_empty() || prefix.is_empty() || prefix.contains('/') {
            return false;
        }
        let Ok(address) = address.parse::<IpAddr>() else {
            return false;
        };
        let Ok(prefix) = prefix.parse::<u8>() else {
            return false;
        };
        prefix <= if address.is_ipv4() { 32 } else { 128 }
    })
}

fn valid_hostname(value: &str) -> bool {
    if value.is_empty() || value.len() > 253 || value.starts_with('.') || value.ends_with('.') {
        return false;
    }
    value.split('.').all(|label| {
        !label.is_empty()
            && label.len() <= 63
            && !label.starts_with('-')
            && !label.ends_with('-')
            && label
                .chars()
                .all(|character| character.is_ascii_alphanumeric() || character == '-')
    })
}

fn valid_https_origin(value: &str) -> bool {
    let Ok(origin) = url::Url::parse(value) else {
        return false;
    };
    origin.scheme() == "https"
        && origin.host_str().is_some()
        && origin.username().is_empty()
        && origin.password().is_none()
        && origin.path() == "/"
        && origin.query().is_none()
        && origin.fragment().is_none()
}

fn validate_secret_name(name: &str) -> Result<(), SetupError> {
    let path = Path::new(name);
    let valid = !name.is_empty()
        && !path.is_absolute()
        && name.split('/').all(is_safe_secret_component)
        && path
            .components()
            .all(|component| matches!(component, Component::Normal(_)));
    if !valid {
        return Err(SetupError::InvalidPayload(format!(
            "invalid secret filename {name}"
        )));
    }
    Ok(())
}

fn is_safe_secret_component(component: &str) -> bool {
    !component.is_empty()
        && component != "."
        && component != ".."
        && component.chars().all(|character| {
            character.is_ascii_lowercase()
                || character.is_ascii_digit()
                || matches!(character, '-' | '_' | '.')
        })
}

pub trait SecretInput<R: BufRead, W: Write> {
    fn read_secret(&mut self, label: &str, reader: &mut R, writer: &mut W) -> io::Result<String>;
}

#[derive(Debug, Default, Clone, Copy)]
pub struct EchoedSecretInput;

impl<R: BufRead, W: Write> SecretInput<R, W> for EchoedSecretInput {
    fn read_secret(&mut self, label: &str, reader: &mut R, writer: &mut W) -> io::Result<String> {
        write!(writer, "{label}: ")?;
        writer.flush()?;
        let mut value = String::new();
        if reader.read_line(&mut value)? == 0 {
            return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "input ended"));
        }
        Ok(value.trim_end_matches(['\r', '\n']).to_owned())
    }
}

#[derive(Debug, Default, Clone, Copy)]
pub struct HiddenSecretInput;

impl<R: BufRead, W: Write> SecretInput<R, W> for HiddenSecretInput {
    fn read_secret(&mut self, label: &str, _reader: &mut R, _writer: &mut W) -> io::Result<String> {
        rpassword::prompt_password(format!("{label}: "))
    }
}

pub struct PromptIo<R, W, S = EchoedSecretInput> {
    reader: R,
    writer: W,
    secret_input: S,
}

impl<R, W> PromptIo<R, W, EchoedSecretInput> {
    pub fn new(reader: R, writer: W) -> Self {
        Self {
            reader,
            writer,
            secret_input: EchoedSecretInput,
        }
    }
}

impl<R, W, S> PromptIo<R, W, S> {
    pub fn with_secret_input(reader: R, writer: W, secret_input: S) -> Self {
        Self {
            reader,
            writer,
            secret_input,
        }
    }
}

impl<R: BufRead, W: Write, S: SecretInput<R, W>> PromptIo<R, W, S> {
    fn line(&mut self, label: &str) -> Result<String, SetupError> {
        write!(self.writer, "{label}: ")?;
        self.writer.flush()?;
        let mut value = String::new();
        if self.reader.read_line(&mut value)? == 0 {
            return Err(SetupError::InputEnded);
        }
        Ok(value.trim_end_matches(['\r', '\n']).to_owned())
    }

    fn required(&mut self, prompt: &RequiredValuePrompt) -> Result<String, SetupError> {
        let label = match &prompt.default {
            Some(default) => format!("{} [{}]", prompt.prompt, default),
            None => prompt.prompt.clone(),
        };
        loop {
            let value = self.line(&label)?;
            let selected = if value.is_empty() {
                if let Some(default) = &prompt.default {
                    default.clone()
                } else {
                    writeln!(self.writer, "A value is required.")?;
                    continue;
                }
            } else {
                value
            };
            if valid_required_value(&selected, &prompt.validation) {
                return Ok(selected);
            }
            writeln!(self.writer, "The value is invalid.")?;
        }
    }

    fn secret<G: SecretGenerator>(
        &mut self,
        prompt: &SecretPrompt,
        generator: &G,
    ) -> Result<String, SetupError> {
        let label = if prompt.generate_bytes.is_some() {
            format!("{} (leave blank to generate)", prompt.prompt)
        } else {
            prompt.prompt.clone()
        };
        loop {
            let value = self
                .secret_input
                .read_secret(&label, &mut self.reader, &mut self.writer)
                .map_err(|error| {
                    if error.kind() == io::ErrorKind::UnexpectedEof {
                        SetupError::InputEnded
                    } else {
                        SetupError::Io(error)
                    }
                })?;
            if !value.is_empty() {
                if prompt.prefix.as_deref().is_some_and(|prefix| {
                    !value.starts_with(prefix)
                        || !value.chars().all(|character| {
                            character.is_ascii_alphanumeric() || "_.~-".contains(character)
                        })
                }) {
                    writeln!(self.writer, "The value is invalid.")?;
                    continue;
                }
                return Ok(value);
            }
            if let Some(bytes) = prompt.generate_bytes {
                let generated = generator.generate(bytes).map_err(SetupError::from)?;
                return Ok(format!(
                    "{}{}",
                    prompt.prefix.as_deref().unwrap_or_default(),
                    generated
                ));
            }
            writeln!(self.writer, "A value is required.")?;
        }
    }

    fn generated_text<G: SecretGenerator>(
        &mut self,
        request: &RandomTextRequest,
        generator: &G,
    ) -> Result<String, SetupError> {
        let value =
            self.read_hidden_value(&format!("{} (leave blank to generate)", request.prompt))?;
        if value.is_empty() {
            generator.generate(request.bytes).map_err(SetupError::from)
        } else {
            validate_single_line_secret(&value, &request.file)?;
            Ok(value)
        }
    }

    fn ed25519_private_key(&mut self, request: &Ed25519KeyRequest) -> Result<String, SetupError> {
        let import_path = self.read_hidden_value(&format!(
            "{} (existing PEM path; leave blank to generate)",
            request.prompt
        ))?;
        if import_path.is_empty() {
            let signing_key = ed25519_dalek::SigningKey::generate(&mut OsRng);
            return Ok(canonical_ed25519_pkcs8_pem(&signing_key));
        }
        let pem = read_import_file(Path::new(&import_path), 64 * 1024)?;
        validate_ed25519_private_key(&pem, &request.file)?;
        Ok(pem)
    }

    fn pki_import_directory(
        &mut self,
        request: &StepCaControllerRequest,
    ) -> Result<Option<PathBuf>, SetupError> {
        let path = self.read_hidden_value(&format!(
            "{} (existing bundle secrets directory; leave blank to generate)",
            request.prompt
        ))?;
        if path.is_empty() {
            Ok(None)
        } else {
            Ok(Some(PathBuf::from(path)))
        }
    }

    fn read_hidden_value(&mut self, label: &str) -> Result<String, SetupError> {
        self.secret_input
            .read_secret(label, &mut self.reader, &mut self.writer)
            .map_err(|error| {
                if error.kind() == io::ErrorKind::UnexpectedEof {
                    SetupError::InputEnded
                } else {
                    SetupError::Io(error)
                }
            })
    }

    fn confirm(&mut self, label: &str) -> Result<bool, SetupError> {
        loop {
            let value = self.line(&format!("{label} [y/N]"))?;
            match value.trim().to_ascii_lowercase().as_str() {
                "y" | "yes" => return Ok(true),
                "" | "n" | "no" => return Ok(false),
                _ => writeln!(self.writer, "Please answer yes or no.")?,
            }
        }
    }
}

fn validate_single_line_secret(value: &str, file: &str) -> Result<(), SetupError> {
    if value.is_empty() || value.contains(['\0', '\r', '\n']) {
        return Err(SetupError::InvalidSecretMaterial(format!(
            "{file} must be a non-empty single-line value"
        )));
    }
    Ok(())
}

fn read_import_file(path: &Path, maximum_bytes: u64) -> Result<String, SetupError> {
    let path = canonicalize_selected_path(path)?;
    let metadata = fs::symlink_metadata(&path).map_err(|error| {
        SetupError::InvalidSecretMaterial(format!("cannot read {}: {error}", path.display()))
    })?;
    if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() > maximum_bytes {
        return Err(SetupError::InvalidSecretMaterial(format!(
            "{} is not a safe regular import file",
            path.display()
        )));
    }
    fs::read_to_string(&path).map_err(|error| {
        SetupError::InvalidSecretMaterial(format!("cannot read {}: {error}", path.display()))
    })
}

fn validate_ed25519_private_key(pem: &str, file: &str) -> Result<(), SetupError> {
    let signing_key = ed25519_dalek::SigningKey::from_pkcs8_pem(pem).map_err(|_| {
        SetupError::InvalidSecretMaterial(format!("{file} is not a PKCS#8 PEM private key"))
    })?;
    if pem.trim_end_matches(['\r', '\n']) != canonical_ed25519_pkcs8_pem(&signing_key) {
        return Err(SetupError::InvalidSecretMaterial(format!(
            "{file} is not a canonical Ed25519 PKCS#8 PEM private key"
        )));
    }
    Ok(())
}

fn canonical_ed25519_pkcs8_pem(signing_key: &ed25519_dalek::SigningKey) -> String {
    // RFC 8410 section 7's version-0 PrivateKeyInfo form is accepted by ring,
    // OpenSSL, and Python cryptography. Some encoders emit the optional public
    // key as a version-1 extension, which cryptography rejects as trailing ASN.1.
    const PREFIX: [u8; 16] = [
        0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04,
        0x20,
    ];
    let mut document = Vec::with_capacity(PREFIX.len() + signing_key.to_bytes().len());
    document.extend_from_slice(&PREFIX);
    document.extend_from_slice(&signing_key.to_bytes());
    format!(
        "-----BEGIN PRIVATE KEY-----\n{}\n-----END PRIVATE KEY-----",
        Base64::encode_string(&document)
    )
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SetupMode {
    Install,
    Upgrade,
}

#[derive(Debug)]
pub struct SetupRequest {
    output_root: PathBuf,
    mode: SetupMode,
    hermes_enabled: Option<bool>,
}

impl SetupRequest {
    pub fn install(output_root: impl AsRef<Path>) -> Self {
        Self {
            output_root: output_root.as_ref().to_path_buf(),
            mode: SetupMode::Install,
            hermes_enabled: None,
        }
    }

    pub fn upgrade(output_root: impl AsRef<Path>) -> Self {
        Self {
            output_root: output_root.as_ref().to_path_buf(),
            mode: SetupMode::Upgrade,
            hermes_enabled: None,
        }
    }

    pub fn with_hermes_enabled(mut self, enabled: bool) -> Self {
        self.hermes_enabled = Some(enabled);
        self
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct SetupOutcome {
    pub root: PathBuf,
    pub hermes_enabled: Option<bool>,
}

pub fn prepare<R: BufRead, W: Write, S: SecretInput<R, W>, G: SecretGenerator>(
    payload: &CanonicalTemplatePayload,
    request: SetupRequest,
    prompt: &mut PromptIo<R, W, S>,
    generator: &G,
) -> Result<SetupOutcome, SetupError> {
    let output_root = ensure_safe_output_root(&request.output_root)?;
    let bundle = output_root.join("vonk-forge");
    match request.mode {
        SetupMode::Install => install(payload, &bundle, request.hermes_enabled, prompt, generator),
        SetupMode::Upgrade => upgrade(payload, &bundle, request.hermes_enabled, prompt, generator),
    }
}

fn install<R: BufRead, W: Write, S: SecretInput<R, W>, G: SecretGenerator>(
    payload: &CanonicalTemplatePayload,
    bundle: &Path,
    requested_hermes_enabled: Option<bool>,
    prompt: &mut PromptIo<R, W, S>,
    generator: &G,
) -> Result<SetupOutcome, SetupError> {
    if fs::symlink_metadata(bundle).is_ok() {
        return Err(SetupError::AlreadyExists);
    }

    let staging = create_staging_directory(bundle.parent().expect("bundle has output parent"))?;
    let result = (|| {
        let mut environment = payload
            .internal_values
            .iter()
            .map(|value| (value.env.clone(), value.value.clone()))
            .collect::<Vec<_>>();
        let mut secret_values = Vec::new();
        for value in &payload.required_values {
            environment.push((value.env.clone(), prompt.required(value)?));
        }
        for secret in &payload.secrets {
            secret_values.push((secret.file.clone(), prompt.secret(secret, generator)?));
        }
        for request in &payload.generated_secrets.random_text {
            secret_values.push((
                request.file.clone(),
                prompt.generated_text(request, generator)?,
            ));
        }
        for request in &payload.generated_secrets.ed25519_pkcs8_pem {
            secret_values.push((request.file.clone(), prompt.ed25519_private_key(request)?));
        }
        for request in &payload.generated_secrets.postgres_urls {
            let password = secret_value(&secret_values, &request.password_file)?.to_owned();
            secret_values.push((
                request.file.clone(),
                render_postgres_url(request, &password)?,
            ));
        }
        if let Some(request) = &payload.step_ca_controller {
            let material = prepare_pki(request, &environment, prompt, generator)?;
            environment.push((request.kid_env.clone(), material.kid));
            secret_values.extend(material.files);
        }

        let hermes_enabled = if let Some(hermes) = &payload.hermes {
            let enabled = match requested_hermes_enabled {
                Some(enabled) => enabled,
                None => prompt.confirm(&hermes.prompt)?,
            };
            environment.push((
                hermes.env.clone(),
                if enabled {
                    hermes.enabled_value.clone()
                } else {
                    hermes.disabled_value.clone()
                },
            ));
            if enabled {
                for value in &hermes.required_values {
                    environment.push((value.env.clone(), prompt.required(value)?));
                }
                for secret in &hermes.secrets {
                    secret_values.push((secret.file.clone(), prompt.secret(secret, generator)?));
                }
            }
            Some(enabled)
        } else {
            None
        };

        write_new_file(
            &staging.join("docker-compose.yaml"),
            payload.docker_compose_yaml.as_bytes(),
            0o644,
        )?;
        let environment = render_owned_environment(&environment)?;
        write_new_file(&staging.join(".env"), environment.as_bytes(), 0o600)?;
        let secret_directory = staging.join("secrets");
        create_secure_directory(&secret_directory)?;
        for (name, value) in secret_values {
            let content = secret_file_content(value);
            write_secret_file(&secret_directory, &name, &content)?;
        }
        for runtime_file in &payload.runtime_files {
            write_runtime_file(
                &secret_directory,
                &runtime_file.file,
                runtime_file.content.as_bytes(),
                runtime_file.mode,
            )?;
        }
        sync_directory(&secret_directory)?;
        sync_directory(&staging)?;
        fs::rename(&staging, bundle)?;
        sync_directory(bundle.parent().expect("bundle has output parent"))?;
        Ok(SetupOutcome {
            root: bundle.to_path_buf(),
            hermes_enabled,
        })
    })();
    if result.is_err() {
        let _ = fs::remove_dir_all(&staging);
    }
    result
}

fn secret_value<'a>(values: &'a [(String, String)], file: &str) -> Result<&'a str, SetupError> {
    values
        .iter()
        .find_map(|(name, value)| (name.as_str() == file).then_some(value.as_str()))
        .ok_or_else(|| {
            SetupError::InvalidPayload(format!("generated secret dependency {file} is unavailable"))
        })
}

fn render_postgres_url(request: &PostgresUrlRequest, password: &str) -> Result<String, SetupError> {
    validate_single_line_secret(password, &request.password_file)?;
    let mut url = url::Url::parse(&format!(
        "{}://{}:{}/",
        request.scheme, request.host, request.port
    ))
    .map_err(|_| {
        SetupError::InvalidPayload(format!(
            "PostgreSQL URL request for {} is invalid",
            request.file
        ))
    })?;
    url.set_username(&request.username).map_err(|_| {
        SetupError::InvalidPayload(format!(
            "PostgreSQL username for {} is invalid",
            request.file
        ))
    })?;
    url.set_password(Some(password)).map_err(|_| {
        SetupError::InvalidSecretMaterial(format!(
            "password for {} cannot be encoded",
            request.file
        ))
    })?;
    url.set_path(&request.database);
    Ok(url.into())
}

fn secret_file_content(value: String) -> Vec<u8> {
    let mut content = value.trim_end_matches(['\r', '\n']).as_bytes().to_vec();
    content.push(b'\n');
    content
}

struct PkiMaterial {
    kid: String,
    files: Vec<(String, String)>,
}

#[derive(Clone, Deserialize, Serialize)]
struct PublicJwk {
    alg: String,
    crv: String,
    kid: String,
    kty: String,
    #[serde(rename = "use")]
    key_use: String,
    x: String,
    y: String,
}

#[derive(Deserialize, Serialize)]
struct PrivateJwk {
    alg: String,
    crv: String,
    d: String,
    kid: String,
    kty: String,
    #[serde(rename = "use")]
    key_use: String,
    x: String,
    y: String,
}

fn prepare_pki<R: BufRead, W: Write, S: SecretInput<R, W>, G: SecretGenerator>(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
    prompt: &mut PromptIo<R, W, S>,
    generator: &G,
) -> Result<PkiMaterial, SetupError> {
    if let Some(directory) = prompt.pki_import_directory(request)? {
        return import_pki(request, environment, &directory);
    }
    generate_pki(request, environment, generator)
}

fn generate_pki<G: SecretGenerator>(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
    generator: &G,
) -> Result<PkiMaterial, SetupError> {
    let hostnames = pki_hostnames(request, environment)?;
    let password = generator.generate(request.password_bytes)?;
    validate_single_line_secret(&password, &request.files.password)?;

    let root_key = KeyPair::generate_for(&PKCS_ED25519)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let root_params = ca_certificate_params("Vonk Forge Root CA", 1, 3650)?;
    let root = CertifiedIssuer::self_signed(root_params, root_key)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;

    let intermediate_key = KeyPair::generate_for(&PKCS_ED25519)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let intermediate_der = intermediate_key.serialize_der();
    let intermediate_params = ca_certificate_params("Vonk Forge Agent Intermediate CA", 0, 1825)?;
    let intermediate = CertifiedIssuer::signed_by(intermediate_params, intermediate_key, &root)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;

    let controller_key = KeyPair::generate_for(&PKCS_ED25519)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let controller_params =
        controller_certificate_params(&hostnames, time::OffsetDateTime::now_utc())?;
    let controller_certificate = controller_params
        .signed_by(&controller_key, &intermediate)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;

    let signing_key = ed25519_dalek::SigningKey::from_pkcs8_der(&intermediate_der)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let plaintext = signing_key
        .to_pkcs8_der()
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let private_key_info = pkcs8::PrivateKeyInfo::try_from(plaintext.as_bytes())
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let mut salt = [0_u8; 16];
    let mut initialization_vector = [0_u8; 16];
    OsRng.fill_bytes(&mut salt);
    OsRng.fill_bytes(&mut initialization_vector);
    let encryption = pkcs8::pkcs5::pbes2::Parameters::pbkdf2_sha256_aes256cbc(
        600_000,
        &salt,
        &initialization_vector,
    )
    .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let encrypted_document = private_key_info
        .encrypt_with_params(encryption, password.as_bytes())
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let encrypted_intermediate = encrypted_document
        .to_pem("ENCRYPTED PRIVATE KEY", LineEnding::LF)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?
        .to_string();
    let (public_jwk, private_jwk) = generate_es256_jwks()?;
    let ca_config = render_ca_config(request, &public_jwk)?;
    let root_pem = root.pem();
    let intermediate_pem = intermediate.pem();
    let controller_chain = format!("{}{}", controller_certificate.pem(), intermediate_pem);
    let files = &request.files;

    Ok(PkiMaterial {
        kid: public_jwk.kid.clone(),
        files: vec![
            (files.root_certificate.clone(), root_pem.clone()),
            (files.intermediate_certificate.clone(), intermediate_pem),
            (
                files.intermediate_private_key.clone(),
                encrypted_intermediate,
            ),
            (
                files.controller_server_certificate.clone(),
                controller_chain,
            ),
            (
                files.controller_server_private_key.clone(),
                controller_key.serialize_pem(),
            ),
            (
                files.provisioner_private_jwk.clone(),
                serde_json::to_string(&private_jwk)
                    .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?,
            ),
            (
                files.provisioner_public_jwk.clone(),
                serde_json::to_string(&public_jwk)
                    .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?,
            ),
            (files.ca_config.clone(), ca_config),
            (files.password.clone(), password),
        ],
    })
}

fn controller_certificate_params(
    hostnames: &[String],
    now: time::OffsetDateTime,
) -> Result<CertificateParams, SetupError> {
    let mut params = CertificateParams::new(hostnames.to_vec()).map_err(|error| {
        SetupError::InvalidPayload(format!("invalid controller hostname: {error}"))
    })?;
    params.not_before = now - time::Duration::hours(1);
    params.not_after = now + time::Duration::days(CONTROLLER_CERTIFICATE_VALIDITY_DAYS);
    params
        .distinguished_name
        .push(DnType::CommonName, hostnames[0].clone());
    params.key_usages.push(KeyUsagePurpose::DigitalSignature);
    params
        .extended_key_usages
        .push(ExtendedKeyUsagePurpose::ServerAuth);
    params.use_authority_key_identifier_extension = true;
    Ok(params)
}

fn ca_certificate_params(
    common_name: &str,
    path_length: u8,
    validity_days: i64,
) -> Result<CertificateParams, SetupError> {
    let mut params = CertificateParams::new(Vec::<String>::new())
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let now = time::OffsetDateTime::now_utc();
    params.not_before = now - time::Duration::hours(1);
    params.not_after = now + time::Duration::days(validity_days);
    params
        .distinguished_name
        .push(DnType::CommonName, common_name);
    params.is_ca = IsCa::Ca(BasicConstraints::Constrained(path_length));
    params.key_usages = vec![
        KeyUsagePurpose::DigitalSignature,
        KeyUsagePurpose::KeyCertSign,
        KeyUsagePurpose::CrlSign,
    ];
    Ok(params)
}

fn pki_hostnames(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
) -> Result<Vec<String>, SetupError> {
    request
        .hostname_envs
        .iter()
        .map(|name| {
            let value = environment_value(environment, name).ok_or_else(|| {
                SetupError::InvalidPayload(format!(
                    "Step CA hostname environment key {name} is unavailable"
                ))
            })?;
            if value.is_empty() || value.contains(['\0', '\r', '\n', '/', ':']) {
                return Err(SetupError::InvalidPayload(format!(
                    "Step CA hostname value for {name} is invalid"
                )));
            }
            Ok(value.to_owned())
        })
        .collect()
}

fn generate_es256_jwks() -> Result<(PublicJwk, PrivateJwk), SetupError> {
    let secret = p256::SecretKey::random(&mut OsRng);
    let point = secret.public_key().to_encoded_point(false);
    let x = Base64UrlUnpadded::encode_string(
        point
            .x()
            .ok_or_else(|| SetupError::InvalidSecretMaterial("P-256 x is missing".to_owned()))?,
    );
    let y = Base64UrlUnpadded::encode_string(
        point
            .y()
            .ok_or_else(|| SetupError::InvalidSecretMaterial("P-256 y is missing".to_owned()))?,
    );
    let canonical = format!("{{\"crv\":\"P-256\",\"kty\":\"EC\",\"x\":\"{x}\",\"y\":\"{y}\"}}");
    let kid = Base64UrlUnpadded::encode_string(&Sha256::digest(canonical.as_bytes()));
    let public = PublicJwk {
        alg: "ES256".to_owned(),
        crv: "P-256".to_owned(),
        kid: kid.clone(),
        kty: "EC".to_owned(),
        key_use: "sig".to_owned(),
        x: x.clone(),
        y: y.clone(),
    };
    let private = PrivateJwk {
        alg: "ES256".to_owned(),
        crv: "P-256".to_owned(),
        d: Base64UrlUnpadded::encode_string(&secret.to_bytes()),
        kid,
        kty: "EC".to_owned(),
        key_use: "sig".to_owned(),
        x,
        y,
    };
    Ok((public, private))
}

fn render_ca_config(
    request: &StepCaControllerRequest,
    public_jwk: &PublicJwk,
) -> Result<String, SetupError> {
    let document = serde_json::json!({
        "root": "/run/vonk-normalized-secrets/step-ca/root-certificate",
        "crt": "/run/vonk-normalized-secrets/step-ca/intermediate-certificate",
        "key": "/run/vonk-normalized-secrets/step-ca/intermediate-key",
        "address": ":9000",
        "insecureAddress": "",
        "dnsNames": ["step-ca"],
        "logger": {"format": "json"},
        "db": {"type": "badgerv2", "dataSource": "/home/step/db"},
        "crl": {
            "enabled": true,
            "generateOnRevoke": true,
            "cacheDuration": "1h",
            "renewPeriod": "30m"
        },
        "authority": {
            "provisioners": [{
                "type": "JWK",
                "name": request.provisioner_name,
                "key": public_jwk,
                "claims": {
                    "minTLSCertDuration": "24h",
                    "maxTLSCertDuration": "24h",
                    "defaultTLSCertDuration": "24h",
                    "disableRenewal": true,
                    "disableSmallstepExtensions": true
                },
                "options": {
                    "x509": {
                        "template": "{\"subject\":{\"commonName\":{{ toJson .Subject.CommonName }}},\"sans\":{{ toJson .SANs }},\"keyUsage\":[\"digitalSignature\"],\"extKeyUsage\":[\"clientAuth\"]}"
                    }
                }
            }]
        }
    });
    serde_json::to_string_pretty(&document)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))
}

fn import_pki(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
    directory: &Path,
) -> Result<PkiMaterial, SetupError> {
    let directory = canonicalize_selected_path(directory)?;
    require_real_directory(&directory).map_err(|_| {
        SetupError::InvalidSecretMaterial(format!(
            "{} is not a safe PKI import directory",
            directory.display()
        ))
    })?;
    let files = request
        .files
        .all()
        .into_iter()
        .map(|relative| {
            read_import_file(&directory.join(relative), 1024 * 1024)
                .map(|content| (relative.to_owned(), content))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let kid = validate_pki_material(request, environment, &files)?;
    Ok(PkiMaterial { kid, files })
}

fn validate_pki_material(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
    files: &[(String, String)],
) -> Result<String, SetupError> {
    let validated = validate_pki_material_at(
        request,
        environment,
        files,
        time::OffsetDateTime::now_utc(),
        false,
    )?;
    Ok(validated.kid)
}

struct ValidatedPkiMaterial {
    kid: String,
    controller_needs_renewal: bool,
}

fn validate_upgrade_pki_material(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
    files: &[(String, String)],
) -> Result<ValidatedPkiMaterial, SetupError> {
    validate_pki_material_at(
        request,
        environment,
        files,
        time::OffsetDateTime::now_utc(),
        true,
    )
}

fn validate_pki_material_at(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
    files: &[(String, String)],
    now: time::OffsetDateTime,
    allow_expired_controller: bool,
) -> Result<ValidatedPkiMaterial, SetupError> {
    let value = |name: &str| {
        files
            .iter()
            .find_map(|(path, value)| (path == name).then_some(value.as_str()))
            .ok_or_else(|| {
                SetupError::InvalidSecretMaterial(format!("PKI member {name} is missing"))
            })
    };
    let paths = &request.files;
    let root_pem = value(&paths.root_certificate)?;
    let intermediate_pem = value(&paths.intermediate_certificate)?;
    let server_pem = value(&paths.controller_server_certificate)?;
    let (root_trailing, root_block) = x509_parser::pem::parse_x509_pem(root_pem.as_bytes())
        .map_err(|_| invalid_pki("root certificate is not valid PEM"))?;
    let (intermediate_trailing, intermediate_block) =
        x509_parser::pem::parse_x509_pem(intermediate_pem.as_bytes())
            .map_err(|_| invalid_pki("intermediate certificate is not valid PEM"))?;
    let (server_chain, server_block) = x509_parser::pem::parse_x509_pem(server_pem.as_bytes())
        .map_err(|_| invalid_pki("controller certificate is not valid PEM"))?;
    let (server_trailing, bundled_intermediate_block) =
        x509_parser::pem::parse_x509_pem(server_chain)
            .map_err(|_| invalid_pki("controller certificate chain has no intermediate"))?;
    if !root_trailing.iter().all(u8::is_ascii_whitespace)
        || !intermediate_trailing.iter().all(u8::is_ascii_whitespace)
        || !server_trailing.iter().all(u8::is_ascii_whitespace)
        || bundled_intermediate_block.contents != intermediate_block.contents
    {
        return Err(invalid_pki("certificate chain is inconsistent"));
    }
    let (_, root) = x509_parser::parse_x509_certificate(&root_block.contents)
        .map_err(|_| invalid_pki("root certificate is not valid X.509"))?;
    let (_, intermediate) = x509_parser::parse_x509_certificate(&intermediate_block.contents)
        .map_err(|_| invalid_pki("intermediate certificate is not valid X.509"))?;
    let (_, server) = x509_parser::parse_x509_certificate(&server_block.contents)
        .map_err(|_| invalid_pki("controller certificate is not valid X.509"))?;
    root.verify_signature(None)
        .map_err(|_| invalid_pki("root certificate is not self-signed"))?;
    intermediate
        .verify_signature(Some(root.public_key()))
        .map_err(|_| invalid_pki("intermediate certificate is not signed by the root"))?;
    server
        .verify_signature(Some(intermediate.public_key()))
        .map_err(|_| invalid_pki("controller certificate is not signed by the intermediate"))?;

    let expected_hostnames = pki_hostnames(request, environment)?;
    let actual_hostnames = server
        .subject_alternative_name()
        .map_err(|_| invalid_pki("controller certificate has an invalid SAN extension"))?
        .ok_or_else(|| invalid_pki("controller certificate has no SAN extension"))?
        .value
        .general_names
        .iter()
        .filter_map(|name| match name {
            x509_parser::extensions::GeneralName::DNSName(value) => Some((*value).to_owned()),
            _ => None,
        })
        .collect::<Vec<_>>();
    if actual_hostnames != expected_hostnames {
        return Err(invalid_pki(
            "controller certificate hostnames do not match the configured hostnames",
        ));
    }

    let server_key = KeyPair::from_pem(value(&paths.controller_server_private_key)?)
        .map_err(|_| invalid_pki("controller private key is not valid PKCS#8 PEM"))?;
    if !server_key.is_compatible(&PKCS_ED25519)
        || server_key.public_key_raw() != server.public_key().subject_public_key.data.as_ref()
    {
        return Err(invalid_pki(
            "controller private key does not match the controller certificate",
        ));
    }
    let password = value(&paths.password)?.trim_end_matches(['\r', '\n']);
    validate_single_line_secret(password, &paths.password)?;
    let intermediate_key = ed25519_dalek::SigningKey::from_pkcs8_encrypted_pem(
        value(&paths.intermediate_private_key)?,
        password.as_bytes(),
    )
    .map_err(|_| invalid_pki("intermediate private key cannot be decrypted"))?;
    if intermediate_key.verifying_key().as_bytes()
        != intermediate.public_key().subject_public_key.data.as_ref()
    {
        return Err(invalid_pki(
            "intermediate private key does not match the intermediate certificate",
        ));
    }

    let public: PublicJwk = serde_json::from_str(value(&paths.provisioner_public_jwk)?)
        .map_err(|_| invalid_pki("public provisioner JWK is invalid"))?;
    let private: PrivateJwk = serde_json::from_str(value(&paths.provisioner_private_jwk)?)
        .map_err(|_| invalid_pki("private provisioner JWK is invalid"))?;
    validate_es256_jwks(&public, &private)?;
    let config: serde_json::Value = serde_json::from_str(value(&paths.ca_config)?)
        .map_err(|_| invalid_pki("Step CA configuration is invalid JSON"))?;
    let public_json = serde_json::to_value(&public)
        .map_err(|_| invalid_pki("public provisioner JWK cannot be represented"))?;
    if config["root"] != "/run/vonk-normalized-secrets/step-ca/root-certificate"
        || config["crt"] != "/run/vonk-normalized-secrets/step-ca/intermediate-certificate"
        || config["key"] != "/run/vonk-normalized-secrets/step-ca/intermediate-key"
        || config["authority"]["provisioners"][0]["name"] != request.provisioner_name
        || config["authority"]["provisioners"][0]["key"] != public_json
    {
        return Err(invalid_pki(
            "Step CA configuration does not describe the imported authority",
        ));
    }

    let now_asn1 = x509_parser::time::ASN1Time::new(now);
    if !root.validity().is_valid_at(now_asn1) || !intermediate.validity().is_valid_at(now_asn1) {
        return Err(invalid_pki("CA certificate is outside its validity period"));
    }
    if server.validity().not_before > now_asn1 {
        return Err(invalid_pki("controller certificate is not valid yet"));
    }
    let controller_expired = server.validity().not_after < now_asn1;
    if controller_expired && !allow_expired_controller {
        return Err(invalid_pki(
            "controller certificate is outside its validity period",
        ));
    }
    let renewal_deadline =
        now + time::Duration::days(CONTROLLER_CERTIFICATE_RENEWAL_THRESHOLD_DAYS);
    Ok(ValidatedPkiMaterial {
        kid: public.kid,
        controller_needs_renewal: server.validity().not_after.timestamp()
            <= renewal_deadline.unix_timestamp(),
    })
}

fn validate_es256_jwks(public: &PublicJwk, private: &PrivateJwk) -> Result<(), SetupError> {
    if public.alg != "ES256"
        || public.crv != "P-256"
        || public.kty != "EC"
        || public.key_use != "sig"
        || private.alg != public.alg
        || private.crv != public.crv
        || private.kid != public.kid
        || private.kty != public.kty
        || private.key_use != public.key_use
        || private.x != public.x
        || private.y != public.y
    {
        return Err(invalid_pki("provisioner JWK metadata is inconsistent"));
    }
    let canonical = format!(
        "{{\"crv\":\"P-256\",\"kty\":\"EC\",\"x\":\"{}\",\"y\":\"{}\"}}",
        public.x, public.y
    );
    let expected_kid = Base64UrlUnpadded::encode_string(&Sha256::digest(canonical.as_bytes()));
    if public.kid != expected_kid {
        return Err(invalid_pki(
            "provisioner JWK kid is not an RFC 7638 thumbprint",
        ));
    }
    let scalar = Base64UrlUnpadded::decode_vec(&private.d)
        .map_err(|_| invalid_pki("private provisioner JWK scalar is invalid"))?;
    let secret = p256::SecretKey::from_slice(&scalar)
        .map_err(|_| invalid_pki("private provisioner JWK scalar is invalid"))?;
    let point = secret.public_key().to_encoded_point(false);
    if Base64UrlUnpadded::encode_string(point.x().ok_or_else(|| invalid_pki("P-256 x is missing"))?)
        != public.x
        || Base64UrlUnpadded::encode_string(
            point.y().ok_or_else(|| invalid_pki("P-256 y is missing"))?,
        ) != public.y
    {
        return Err(invalid_pki(
            "private provisioner JWK does not match the public JWK",
        ));
    }
    Ok(())
}

fn invalid_pki(message: &str) -> SetupError {
    SetupError::InvalidSecretMaterial(format!("Step CA/controller PKI {message}"))
}

struct ControllerLeafReplacement {
    certificate_path: String,
    certificate: String,
    private_key_path: String,
    private_key: String,
}

fn renew_controller_leaf(
    request: &StepCaControllerRequest,
    environment: &[(String, String)],
    files: &[(String, String)],
) -> Result<ControllerLeafReplacement, SetupError> {
    let value = |name: &str| {
        files
            .iter()
            .find_map(|(path, value)| (path == name).then_some(value.as_str()))
            .ok_or_else(|| {
                SetupError::InvalidSecretMaterial(format!("PKI member {name} is missing"))
            })
    };
    let paths = &request.files;
    let password = value(&paths.password)?.trim_end_matches(['\r', '\n']);
    let intermediate_signing_key = ed25519_dalek::SigningKey::from_pkcs8_encrypted_pem(
        value(&paths.intermediate_private_key)?,
        password.as_bytes(),
    )
    .map_err(|_| invalid_pki("intermediate private key cannot be decrypted"))?;
    let intermediate_der = intermediate_signing_key
        .to_pkcs8_der()
        .map_err(|_| invalid_pki("intermediate private key cannot be represented"))?;
    let intermediate_key = KeyPair::try_from(intermediate_der.as_bytes())
        .map_err(|_| invalid_pki("intermediate private key cannot be represented"))?;
    let intermediate_pem = value(&paths.intermediate_certificate)?;
    let issuer = Issuer::from_ca_cert_pem(intermediate_pem, intermediate_key)
        .map_err(|_| invalid_pki("intermediate certificate cannot issue a controller leaf"))?;
    let hostnames = pki_hostnames(request, environment)?;
    let controller_key = KeyPair::generate_for(&PKCS_ED25519)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let controller_params =
        controller_certificate_params(&hostnames, time::OffsetDateTime::now_utc())?;
    let controller_certificate = controller_params
        .signed_by(&controller_key, &issuer)
        .map_err(|error| SetupError::InvalidSecretMaterial(error.to_string()))?;
    let replacement = ControllerLeafReplacement {
        certificate_path: paths.controller_server_certificate.clone(),
        certificate: format!("{}{}", controller_certificate.pem(), intermediate_pem),
        private_key_path: paths.controller_server_private_key.clone(),
        private_key: controller_key.serialize_pem(),
    };

    let mut candidate = files.to_vec();
    for (path, content) in [
        (&replacement.certificate_path, &replacement.certificate),
        (&replacement.private_key_path, &replacement.private_key),
    ] {
        let (_, existing) = candidate
            .iter_mut()
            .find(|(candidate_path, _)| candidate_path == path)
            .ok_or_else(|| {
                SetupError::InvalidSecretMaterial(format!("PKI member {path} is missing"))
            })?;
        *existing = content.clone();
    }
    validate_pki_material(request, environment, &candidate)?;
    Ok(replacement)
}

fn upgrade<R: BufRead, W: Write, S: SecretInput<R, W>, G: SecretGenerator>(
    payload: &CanonicalTemplatePayload,
    bundle: &Path,
    requested_hermes_enabled: Option<bool>,
    prompt: &mut PromptIo<R, W, S>,
    generator: &G,
) -> Result<SetupOutcome, SetupError> {
    validate_existing_bundle(bundle)?;
    let mut environment = parse_environment(&bundle.join(".env"))?;
    for internal in &payload.internal_values {
        set_environment_value(&mut environment, &internal.env, internal.value.clone());
    }
    let mut new_secrets = Vec::new();
    let mut controller_leaf_replacement = None;
    for required in &payload.required_values {
        if environment_value(&environment, &required.env).is_none() {
            environment.push((required.env.clone(), prompt.required(required)?));
        }
    }
    collect_missing_secrets(
        &bundle.join("secrets"),
        &payload.secrets,
        prompt,
        generator,
        &mut new_secrets,
    )?;
    collect_missing_generated_secrets(
        &bundle.join("secrets"),
        &payload.generated_secrets,
        prompt,
        generator,
        &mut new_secrets,
    )?;
    if let Some(request) = &payload.step_ca_controller {
        let secret_root = bundle.join("secrets");
        let existing = request
            .files
            .all()
            .into_iter()
            .map(|file| secret_file_exists(&secret_root, file))
            .collect::<Result<Vec<_>, _>>()?;
        let existing_count = existing.into_iter().filter(|present| *present).count();
        match existing_count {
            0 => {
                let material = prepare_pki(request, &environment, prompt, generator)?;
                set_environment_value(&mut environment, &request.kid_env, material.kid);
                new_secrets.extend(material.files);
            }
            count if count == request.files.all().len() => {
                let files = request
                    .files
                    .all()
                    .into_iter()
                    .map(|file| {
                        read_existing_secret(&secret_root, file)
                            .map(|content| (file.to_owned(), content))
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                let validated = validate_upgrade_pki_material(request, &environment, &files)?;
                set_environment_value(&mut environment, &request.kid_env, validated.kid);
                if validated.controller_needs_renewal {
                    controller_leaf_replacement =
                        Some(renew_controller_leaf(request, &environment, &files)?);
                }
            }
            _ => {
                return Err(SetupError::InvalidSecretMaterial(
                    "partial Step CA/controller PKI group cannot be upgraded".to_owned(),
                ));
            }
        }
    }

    let hermes_enabled = if let Some(hermes) = &payload.hermes {
        let current = match environment_value(&environment, &hermes.env) {
            Some(value) if value == hermes.enabled_value => true,
            Some(value) if value == hermes.disabled_value => false,
            Some(_) => {
                return Err(SetupError::InvalidPayload(format!(
                    "existing {} value is not true or false",
                    hermes.env
                )));
            }
            None => match requested_hermes_enabled {
                Some(enabled) => enabled,
                None => prompt.confirm(&hermes.prompt)?,
            },
        };
        let enabled = requested_hermes_enabled.unwrap_or(current);
        set_environment_value(
            &mut environment,
            &hermes.env,
            if enabled {
                hermes.enabled_value.clone()
            } else {
                hermes.disabled_value.clone()
            },
        );
        if enabled {
            for required in &hermes.required_values {
                if environment_value(&environment, &required.env).is_none() {
                    environment.push((required.env.clone(), prompt.required(required)?));
                }
            }
            collect_missing_secrets(
                &bundle.join("secrets"),
                &hermes.secrets,
                prompt,
                generator,
                &mut new_secrets,
            )?;
        }
        Some(enabled)
    } else {
        None
    };

    let environment_document = render_owned_environment(&environment)?;
    for (name, value) in new_secrets {
        let content = secret_file_content(value);
        write_secret_file(&bundle.join("secrets"), &name, &content)?;
    }
    for runtime_file in &payload.runtime_files {
        replace_runtime_file(
            &bundle.join("secrets"),
            &runtime_file.file,
            runtime_file.content.as_bytes(),
            runtime_file.mode,
        )?;
    }
    if let Some(replacement) = controller_leaf_replacement {
        atomic_replace_controller_leaf(&bundle.join("secrets"), replacement)?;
    }
    atomic_replace(&bundle.join(".env"), environment_document.as_bytes(), 0o600)?;
    atomic_replace(
        &bundle.join("docker-compose.yaml"),
        payload.docker_compose_yaml.as_bytes(),
        0o644,
    )?;
    Ok(SetupOutcome {
        root: bundle.to_path_buf(),
        hermes_enabled,
    })
}

fn collect_missing_generated_secrets<
    R: BufRead,
    W: Write,
    S: SecretInput<R, W>,
    G: SecretGenerator,
>(
    secret_root: &Path,
    generated: &GeneratedSecrets,
    prompt: &mut PromptIo<R, W, S>,
    generator: &G,
    new_secrets: &mut Vec<(String, String)>,
) -> Result<(), SetupError> {
    for request in &generated.random_text {
        if !secret_file_exists(secret_root, &request.file)? {
            new_secrets.push((
                request.file.clone(),
                prompt.generated_text(request, generator)?,
            ));
        }
    }
    for request in &generated.ed25519_pkcs8_pem {
        if secret_file_exists(secret_root, &request.file)? {
            let existing = read_existing_secret(secret_root, &request.file)?;
            validate_ed25519_private_key(&existing, &request.file)?;
        } else {
            new_secrets.push((request.file.clone(), prompt.ed25519_private_key(request)?));
        }
    }
    for request in &generated.postgres_urls {
        if secret_file_exists(secret_root, &request.file)? {
            continue;
        }
        let password = if let Some(value) = new_secrets
            .iter()
            .find_map(|(name, value)| (name == &request.password_file).then_some(value.as_str()))
        {
            value.to_owned()
        } else {
            read_existing_secret(secret_root, &request.password_file)?
        };
        new_secrets.push((
            request.file.clone(),
            render_postgres_url(request, &password)?,
        ));
    }
    Ok(())
}

fn secret_file_exists(root: &Path, relative: &str) -> Result<bool, SetupError> {
    let path = root.join(relative);
    match fs::symlink_metadata(&path) {
        Ok(metadata) if metadata.is_file() && !metadata.file_type().is_symlink() => Ok(true),
        Ok(_) => Err(SetupError::UnsafeDestination(format!(
            "{} is not a regular secret file",
            path.display()
        ))),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.into()),
    }
}

fn read_existing_secret(root: &Path, relative: &str) -> Result<String, SetupError> {
    let path = root.join(relative);
    let metadata = fs::metadata(&path)?;
    if metadata.len() > 256 * 1024 {
        return Err(SetupError::UnsafeDestination(format!(
            "{} is too large",
            path.display()
        )));
    }
    Ok(fs::read_to_string(path)?
        .trim_end_matches(['\r', '\n'])
        .to_owned())
}

fn collect_missing_secrets<R: BufRead, W: Write, S: SecretInput<R, W>, G: SecretGenerator>(
    secret_root: &Path,
    required: &[SecretPrompt],
    prompt: &mut PromptIo<R, W, S>,
    generator: &G,
    new_secrets: &mut Vec<(String, String)>,
) -> Result<(), SetupError> {
    for secret in required {
        let path = secret_root.join(&secret.file);
        match fs::symlink_metadata(&path) {
            Ok(metadata) if metadata.is_file() && !metadata.file_type().is_symlink() => {}
            Ok(_) => {
                return Err(SetupError::UnsafeDestination(format!(
                    "{} is not a regular secret file",
                    path.display()
                )));
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                new_secrets.push((secret.file.clone(), prompt.secret(secret, generator)?));
            }
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

fn parse_environment(path: &Path) -> Result<Vec<(String, String)>, SetupError> {
    let document = fs::read_to_string(path)?;
    if document.len() > 256 * 1024 || document.contains('\0') {
        return Err(SetupError::UnsafeDestination(
            "existing .env is too large or malformed".to_owned(),
        ));
    }
    let mut values = Vec::new();
    let mut names = HashSet::new();
    for line in document.lines() {
        let (name, raw_value) = line.split_once('=').ok_or_else(|| {
            SetupError::UnsafeDestination("existing .env has an invalid line".to_owned())
        })?;
        validate_env_name(name)?;
        if !names.insert(name.to_owned()) {
            return Err(SetupError::UnsafeDestination(format!(
                "existing .env repeats {name}"
            )));
        }
        let value = if raw_value.starts_with('"') {
            serde_json::from_str::<String>(raw_value).map_err(|_| {
                SetupError::UnsafeDestination(format!(
                    "existing .env value for {name} is malformed"
                ))
            })?
        } else {
            raw_value.to_owned()
        };
        values.push((name.to_owned(), value));
    }
    Ok(values)
}

fn environment_value<'a>(values: &'a [(String, String)], name: &str) -> Option<&'a str> {
    values
        .iter()
        .find_map(|(key, value)| (key == name).then_some(value.as_str()))
}

fn set_environment_value(values: &mut Vec<(String, String)>, name: &str, value: String) {
    if let Some((_, existing)) = values.iter_mut().find(|(key, _)| key == name) {
        *existing = value;
    } else {
        values.push((name.to_owned(), value));
    }
}

fn render_owned_environment(values: &[(String, String)]) -> Result<String, SetupError> {
    let borrowed = values
        .iter()
        .map(|(key, value)| (key, value.clone()))
        .collect::<Vec<_>>();
    render_environment(&borrowed)
}

fn render_environment(values: &[(&String, String)]) -> Result<String, SetupError> {
    let mut rendered = String::new();
    for (key, value) in values {
        if value.contains('\0') {
            return Err(SetupError::InvalidPayload(format!(
                "value for {key} contains NUL"
            )));
        }
        let encoded = if value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || "._-:/".contains(character))
        {
            value.clone()
        } else {
            serde_json::to_string(value)
                .map_err(|error| SetupError::InvalidPayload(error.to_string()))?
        };
        rendered.push_str(key);
        rendered.push('=');
        rendered.push_str(&encoded);
        rendered.push('\n');
    }
    Ok(rendered)
}

fn ensure_safe_output_root(output: &Path) -> Result<PathBuf, SetupError> {
    if output.as_os_str().is_empty() {
        return Err(SetupError::UnsafeDestination("path is empty".to_owned()));
    }
    let output = canonicalize_selected_path(output)?;
    match fs::symlink_metadata(&output) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
        Ok(_) => {
            return Err(SetupError::UnsafeDestination(
                "output root is not a real directory".to_owned(),
            ));
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            fs::create_dir_all(&output)?;
            set_directory_mode(&output)?;
        }
        Err(error) => return Err(error.into()),
    }
    let output = fs::canonicalize(output)?;
    match fs::symlink_metadata(&output) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => Ok(output),
        _ => Err(SetupError::UnsafeDestination(
            "output root is not a real directory".to_owned(),
        )),
    }
}

fn canonicalize_selected_path(path: &Path) -> Result<PathBuf, SetupError> {
    let requested = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut absolute = PathBuf::new();
    for component in requested.components() {
        match component {
            Component::ParentDir => {
                return Err(SetupError::UnsafeDestination(
                    "parent traversal is not allowed".to_owned(),
                ));
            }
            Component::CurDir => {}
            _ => absolute.push(component.as_os_str()),
        }
    }

    match fs::symlink_metadata(&absolute) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            return Err(SetupError::UnsafeDestination(format!(
                "{} is a symbolic link",
                absolute.display()
            )));
        }
        Ok(_) => return fs::canonicalize(absolute).map_err(SetupError::from),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }

    let mut ancestor = absolute.as_path();
    let mut missing = Vec::new();
    loop {
        let name = ancestor.file_name().ok_or_else(|| {
            SetupError::UnsafeDestination("path has no existing ancestor".to_owned())
        })?;
        missing.push(name.to_os_string());
        ancestor = ancestor.parent().ok_or_else(|| {
            SetupError::UnsafeDestination("path has no existing ancestor".to_owned())
        })?;
        match fs::symlink_metadata(ancestor) {
            Ok(_) => {
                let mut canonical = fs::canonicalize(ancestor)?;
                if !fs::metadata(&canonical)?.is_dir() {
                    return Err(SetupError::UnsafeDestination(format!(
                        "{} is not a directory",
                        ancestor.display()
                    )));
                }
                for component in missing.iter().rev() {
                    canonical.push(component);
                }
                return Ok(canonical);
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
}

fn validate_existing_bundle(bundle: &Path) -> Result<(), SetupError> {
    let bundle = canonicalize_selected_path(bundle)?;
    require_real_directory(&bundle)?;
    for entry in fs::read_dir(&bundle)? {
        let entry = entry?;
        let name = entry.file_name();
        if name != ".env" && name != "docker-compose.yaml" && name != "secrets" {
            return Err(SetupError::UnsafeDestination(format!(
                "{} is an unexpected top-level entry",
                entry.path().display()
            )));
        }
    }
    require_regular_file(&bundle.join("docker-compose.yaml"))?;
    require_regular_file(&bundle.join(".env"))?;
    let secrets = bundle.join("secrets");
    require_real_directory(&secrets)?;
    validate_secret_tree(&secrets)?;
    Ok(())
}

fn validate_secret_tree(directory: &Path) -> Result<(), SetupError> {
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let path = entry.path();
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| SetupError::UnsafeDestination("non-UTF-8 secret path".to_owned()))?;
        if !is_safe_secret_component(&name) {
            return Err(SetupError::UnsafeDestination(format!(
                "{} has an unsafe path component",
                path.display()
            )));
        }
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() {
            return Err(SetupError::UnsafeDestination(format!(
                "{} is a symbolic link",
                path.display()
            )));
        }
        if metadata.is_dir() {
            validate_secret_tree(&path)?;
        } else if !metadata.is_file() {
            return Err(SetupError::UnsafeDestination(format!(
                "{} is not a regular secret file",
                path.display()
            )));
        }
    }
    Ok(())
}

fn require_real_directory(path: &Path) -> Result<(), SetupError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => Ok(()),
        _ => Err(SetupError::MissingBundle),
    }
}

fn require_regular_file(path: &Path) -> Result<(), SetupError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_file() && !metadata.file_type().is_symlink() => Ok(()),
        _ => Err(SetupError::MissingBundle),
    }
}

fn create_staging_directory(parent: &Path) -> Result<PathBuf, SetupError> {
    for _ in 0..32 {
        let sequence = STAGING_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let candidate = parent.join(format!(
            ".vonk-forge.setup-{}-{sequence}",
            std::process::id()
        ));
        match fs::create_dir(&candidate) {
            Ok(()) => {
                set_directory_mode(&candidate)?;
                return Ok(candidate);
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.into()),
        }
    }
    Err(SetupError::UnsafeDestination(
        "could not reserve a staging directory".to_owned(),
    ))
}

fn create_secure_directory(path: &Path) -> Result<(), SetupError> {
    fs::create_dir(path)?;
    set_directory_mode(path)?;
    Ok(())
}

fn write_secret_file(root: &Path, relative: &str, content: &[u8]) -> Result<(), SetupError> {
    write_nested_file(root, relative, content, 0o600)
}

fn write_runtime_file(
    root: &Path,
    relative: &str,
    content: &[u8],
    mode: u32,
) -> Result<(), SetupError> {
    write_nested_file(root, relative, content, mode)
}

fn write_nested_file(
    root: &Path,
    relative: &str,
    content: &[u8],
    mode: u32,
) -> Result<(), SetupError> {
    let path = Path::new(relative);
    ensure_nested_parent(root, path, relative)?;
    let target = root.join(path);
    write_new_file(&target, content, mode)?;
    sync_directory(target.parent().expect("secret has parent"))?;
    Ok(())
}

fn ensure_nested_parent(root: &Path, path: &Path, relative: &str) -> Result<(), SetupError> {
    let parent = path.parent().unwrap_or_else(|| Path::new(""));
    let mut current = root.to_path_buf();
    for component in parent.components() {
        let Component::Normal(component) = component else {
            return Err(SetupError::InvalidPayload(format!(
                "invalid secret path {relative}"
            )));
        };
        current.push(component);
        match fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
            Ok(_) => {
                return Err(SetupError::UnsafeDestination(format!(
                    "{} is not a real directory",
                    current.display()
                )));
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                create_secure_directory(&current)?;
            }
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

fn replace_runtime_file(
    root: &Path,
    relative: &str,
    content: &[u8],
    mode: u32,
) -> Result<(), SetupError> {
    let relative_path = Path::new(relative);
    ensure_nested_parent(root, relative_path, relative)?;
    let target = root.join(relative_path);
    match fs::symlink_metadata(&target) {
        Ok(metadata) if metadata.is_file() && !metadata.file_type().is_symlink() => {
            atomic_replace(&target, content, mode)
        }
        Ok(_) => Err(SetupError::UnsafeDestination(format!(
            "{} is not a regular runtime file",
            target.display()
        ))),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            write_runtime_file(root, relative, content, mode)
        }
        Err(error) => Err(error.into()),
    }
}

fn write_new_file(path: &Path, content: &[u8], mode: u32) -> Result<(), SetupError> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    set_open_mode(&mut options, mode);
    let mut file = options.open(path)?;
    file.write_all(content)?;
    sync_file(&file)?;
    set_file_mode(path, mode)?;
    Ok(())
}

fn stage_replacement(path: &Path, content: &[u8], mode: u32) -> Result<PathBuf, SetupError> {
    let parent = path.parent().expect("file has parent");
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| SetupError::UnsafeDestination("replacement path is invalid".to_owned()))?;
    let temporary = parent.join(format!(
        ".{file_name}.tmp-{}-{}",
        std::process::id(),
        STAGING_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    write_new_file(&temporary, content, mode)?;
    Ok(temporary)
}

fn atomic_replace_controller_leaf(
    secret_root: &Path,
    replacement: ControllerLeafReplacement,
) -> Result<(), SetupError> {
    let certificate_path = secret_root.join(&replacement.certificate_path);
    let private_key_path = secret_root.join(&replacement.private_key_path);
    require_regular_file(&certificate_path)?;
    require_regular_file(&private_key_path)?;
    let parent = certificate_path.parent().expect("certificate has parent");
    if certificate_path == private_key_path
        || private_key_path.parent().expect("private key has parent") != parent
    {
        return Err(SetupError::UnsafeDestination(
            "controller certificate and key replacements must be distinct files in one directory"
                .to_owned(),
        ));
    }

    let old_certificate = fs::read(&certificate_path)?;
    let certificate_content = secret_file_content(replacement.certificate);
    let private_key_content = secret_file_content(replacement.private_key);
    let staged_certificate = stage_replacement(&certificate_path, &certificate_content, 0o600)?;
    let staged_private_key = match stage_replacement(&private_key_path, &private_key_content, 0o600)
    {
        Ok(path) => path,
        Err(error) => {
            let _ = fs::remove_file(staged_certificate);
            return Err(error);
        }
    };

    if let Err(error) = fs::rename(&staged_certificate, &certificate_path) {
        let _ = fs::remove_file(staged_certificate);
        let _ = fs::remove_file(staged_private_key);
        return Err(error.into());
    }
    if let Err(error) = fs::rename(&staged_private_key, &private_key_path) {
        let _ = fs::remove_file(staged_private_key);
        if let Err(rollback_error) = atomic_replace(&certificate_path, &old_certificate, 0o600) {
            return Err(SetupError::UnsafeDestination(format!(
                "controller certificate/key replacement failed ({error}) and certificate rollback failed ({rollback_error})"
            )));
        }
        return Err(error.into());
    }
    sync_directory(parent)?;
    Ok(())
}

fn atomic_replace(path: &Path, content: &[u8], mode: u32) -> Result<(), SetupError> {
    require_regular_file(path)?;
    let parent = path.parent().expect("file has parent");
    let temporary = stage_replacement(path, content, mode)?;
    match fs::rename(&temporary, path) {
        Ok(()) => {
            sync_directory(parent)?;
            Ok(())
        }
        Err(error) => {
            let _ = fs::remove_file(temporary);
            Err(error.into())
        }
    }
}

fn sync_directory(path: &Path) -> Result<(), SetupError> {
    let directory = File::open(path)?;
    sync_file(&directory)
}

fn sync_file(file: &File) -> Result<(), SetupError> {
    complete_sync(file.sync_all(), || {
        rustix::fs::fsync(file).map_err(|error| io::Error::from_raw_os_error(error.raw_os_error()))
    })
}

fn complete_sync<F>(full_sync: io::Result<()>, posix_sync: F) -> Result<(), SetupError>
where
    F: FnOnce() -> io::Result<()>,
{
    match full_sync {
        Ok(()) => Ok(()),
        // std uses F_FULLFSYNC on macOS. SMB mounts can reject that stronger
        // operation with ENOTSUP while supporting POSIX fsync, which still
        // flushes file data and metadata to the NAS. Preserve the durability
        // boundary by falling back to fsync instead of skipping synchronization.
        Err(error) if full_sync_is_unsupported(&error) => posix_sync().map_err(Into::into),
        Err(error) => Err(error.into()),
    }
}

fn full_sync_is_unsupported(error: &io::Error) -> bool {
    error.kind() == io::ErrorKind::Unsupported
        || (cfg!(target_os = "macos") && error.raw_os_error() == Some(45))
}

#[cfg(unix)]
fn set_open_mode(options: &mut OpenOptions, mode: u32) {
    use std::os::unix::fs::OpenOptionsExt;
    options.mode(mode);
}

#[cfg(not(unix))]
fn set_open_mode(_options: &mut OpenOptions, _mode: u32) {}

#[cfg(unix)]
fn set_directory_mode(path: &Path) -> Result<(), SetupError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}

#[cfg(not(unix))]
fn set_directory_mode(_path: &Path) -> Result<(), SetupError> {
    Ok(())
}

#[cfg(unix)]
fn set_file_mode(path: &Path, mode: u32) -> Result<(), SetupError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))?;
    Ok(())
}

#[cfg(not(unix))]
fn set_file_mode(_path: &Path, _mode: u32) -> Result<(), SetupError> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{SetupError, complete_sync};
    use std::cell::Cell;
    use std::io;

    #[test]
    fn unsupported_full_sync_falls_back_to_posix_fsync() {
        let fallback_called = Cell::new(false);
        complete_sync(
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "full sync is unavailable",
            )),
            || {
                fallback_called.set(true);
                Ok(())
            },
        )
        .expect("POSIX fsync preserves the durability boundary");
        assert!(fallback_called.get());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_enotsup_falls_back_even_when_rust_does_not_classify_it() {
        let fallback_called = Cell::new(false);
        complete_sync(Err(io::Error::from_raw_os_error(45)), || {
            fallback_called.set(true);
            Ok(())
        })
        .expect("macOS SMB ENOTSUP falls back to POSIX fsync");
        assert!(fallback_called.get());
    }

    #[test]
    fn full_sync_rejects_other_io_failures_without_fallback() {
        let fallback_called = Cell::new(false);
        let error = complete_sync(
            Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "file cannot be synced",
            )),
            || {
                fallback_called.set(true);
                Ok(())
            },
        )
        .expect_err("real sync failures remain fatal");
        assert!(!fallback_called.get());
        assert!(
            matches!(error, SetupError::Io(inner) if inner.kind() == io::ErrorKind::PermissionDenied)
        );
    }

    #[test]
    fn posix_fsync_failure_remains_fatal() {
        let error = complete_sync(
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "full sync is unavailable",
            )),
            || Err(io::Error::other("POSIX fsync failed")),
        )
        .expect_err("fallback sync failures remain fatal");
        assert!(matches!(error, SetupError::Io(_)));
    }
}
