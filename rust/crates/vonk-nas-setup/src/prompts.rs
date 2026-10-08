//! Prompts.

use super::*;

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
    pub(super) reader: R,
    pub(super) writer: W,
    pub(super) secret_input: S,
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
    pub(super) fn preflight(&mut self, items: &[String]) -> Result<(), SetupError> {
        if items.is_empty() {
            return Ok(());
        }
        writeln!(self.writer, "Before continuing, complete this preflight:")?;
        for item in items {
            writeln!(self.writer, "  [ ] {item}")?;
        }
        writeln!(self.writer)?;
        self.writer.flush()?;
        Ok(())
    }

    pub(super) fn line(&mut self, label: &str) -> Result<String, SetupError> {
        write!(self.writer, "{label}: ")?;
        self.writer.flush()?;
        let mut value = String::new();
        if self.reader.read_line(&mut value)? == 0 {
            return Err(SetupError::InputEnded);
        }
        Ok(value.trim_end_matches(['\r', '\n']).to_owned())
    }

    pub(super) fn required(&mut self, prompt: &RequiredValuePrompt) -> Result<String, SetupError> {
        self.required_with_default(prompt, prompt.default.as_deref())
    }

    pub(super) fn required_with_default(
        &mut self,
        prompt: &RequiredValuePrompt,
        default: Option<&str>,
    ) -> Result<String, SetupError> {
        let label = match default {
            Some(default) => format!("{} [{}]", prompt.prompt, default),
            None => prompt.prompt.clone(),
        };
        loop {
            let value = self.line(&label)?;
            let selected = if value.is_empty() {
                if let Some(default) = default {
                    default.to_owned()
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

    pub(super) fn secret(&mut self, prompt: &SecretPrompt) -> Result<String, SetupError> {
        loop {
            let value = self.read_hidden_value(&prompt.prompt)?;
            if validate_single_line_secret(&value, &prompt.file).is_ok() {
                return Ok(value);
            }
            writeln!(self.writer, "A value is required.")?;
        }
    }

    pub(super) fn optional_secret(&mut self, prompt: &SecretPrompt) -> Result<String, SetupError> {
        let label = if prompt.prompt.to_ascii_lowercase().contains("optional") {
            prompt.prompt.clone()
        } else {
            format!("{} (optional; leave blank to skip)", prompt.prompt)
        };
        loop {
            let value = self.read_hidden_value(&label)?;
            if value.is_empty() || validate_single_line_secret(&value, &prompt.file).is_ok() {
                return Ok(value);
            }
            writeln!(self.writer, "The value is invalid.")?;
        }
    }

    pub(super) fn install_mode(&mut self, modes: &InstallModes) -> Result<bool, SetupError> {
        loop {
            // The default is listed first; an empty answer selects it.
            let other = if modes.default == modes.lab_value {
                &modes.secure_remote_value
            } else {
                &modes.lab_value
            };
            let value = self.line(&format!("{} [{} / {}]", modes.prompt, modes.default, other))?;
            let selected = if value.trim().is_empty() {
                modes.default.as_str()
            } else {
                value.trim()
            };
            if selected == modes.lab_value {
                return Ok(true);
            }
            if selected == modes.secure_remote_value {
                return Ok(false);
            }
            writeln!(
                self.writer,
                "Choose {} or {}.",
                modes.lab_value, modes.secure_remote_value
            )?;
        }
    }

    pub(super) fn read_hidden_value(&mut self, label: &str) -> Result<String, SetupError> {
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

    pub(super) fn note(&mut self, line: &str) -> Result<(), SetupError> {
        writeln!(self.writer, "{line}")?;
        Ok(())
    }

    pub(super) fn confirm(&mut self, label: &str) -> Result<bool, SetupError> {
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

pub(super) fn validate_single_line_secret(value: &str, file: &str) -> Result<(), SetupError> {
    if value.is_empty() || value.contains(['\0', '\r', '\n']) {
        return Err(SetupError::InvalidSecretMaterial(format!(
            "{file} must be a non-empty single-line value"
        )));
    }
    Ok(())
}

pub(super) fn validate_ed25519_private_key(pem: &str, file: &str) -> Result<(), SetupError> {
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

pub(super) fn random_bytes<const N: usize>() -> Result<[u8; N], SetupError> {
    let mut bytes = [0_u8; N];
    SystemRandom::new().fill(&mut bytes).map_err(|_| {
        SetupError::InvalidSecretMaterial("the system random generator failed".to_owned())
    })?;
    Ok(bytes)
}

pub(super) fn generate_ed25519_key() -> Result<ed25519_dalek::SigningKey, SetupError> {
    Ok(ed25519_dalek::SigningKey::from_bytes(&random_bytes()?))
}

pub(super) fn canonical_ed25519_pkcs8_pem(signing_key: &ed25519_dalek::SigningKey) -> String {
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
