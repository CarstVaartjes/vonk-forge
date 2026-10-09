//! Environment.

use super::*;

/// Keep only the `.env` keys this payload names and return the dropped ones,
/// so settings retired by a release do not linger across upgrades.
pub(super) fn retain_known_environment(
    payload: &CanonicalTemplatePayload,
    environment: &mut Vec<(String, String)>,
) -> Vec<String> {
    let mut known = payload
        .internal_values
        .iter()
        .map(|value| value.env.as_str())
        .chain(
            payload
                .required_values
                .iter()
                .map(|value| value.env.as_str()),
        )
        .chain(payload.optional_values.iter().map(String::as_str))
        .chain(payload.hermes.iter().map(|hermes| hermes.env.as_str()))
        .collect::<HashSet<_>>();
    if let Some(modes) = &payload.install_modes {
        known.insert("COMPOSE_PROFILES");
        known.extend(modes.lab_values.iter().map(|value| value.env.as_str()));
    }
    let mut dropped = Vec::new();
    environment.retain(|(key, _)| {
        let keep = known.contains(key.as_str());
        if !keep {
            dropped.push(key.clone());
        }
        keep
    });
    dropped
}

pub(super) fn secret_file_exists(root: &Path, relative: &str) -> Result<bool, SetupError> {
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

pub(super) fn read_existing_secret(root: &Path, relative: &str) -> Result<String, SetupError> {
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

pub(super) fn parse_environment(path: &Path) -> Result<Vec<(String, String)>, SetupError> {
    // Stored configuration is a projection. Salvage unambiguous values and
    // leave unavailable values to the current kit's normal preparation path.
    let document = match fs::symlink_metadata(path) {
        Ok(metadata)
            if metadata.is_file()
                && !metadata.file_type().is_symlink()
                && metadata.len() <= 256 * 1024 =>
        {
            fs::read_to_string(path)?
        }
        _ => return Ok(Vec::new()),
    };
    let mut values = Vec::new();
    let mut ambiguous = HashSet::new();
    for line in document.lines() {
        let Some((name, raw_value)) = line.split_once('=') else {
            continue;
        };
        if validate_env_name(name).is_err() || raw_value.contains('\0') {
            continue;
        }
        let value = if raw_value.starts_with('"') {
            let Ok(value) = serde_json::from_str::<String>(raw_value) else {
                continue;
            };
            value
        } else {
            raw_value.to_owned()
        };
        if value.contains('\0') {
            continue;
        }
        if let Some((_, previous)) = values.iter().find(|(key, _)| key == name) {
            if previous != &value {
                ambiguous.insert(name.to_owned());
            }
        } else {
            values.push((name.to_owned(), value));
        }
    }
    values.retain(|(key, _)| !ambiguous.contains(key));
    Ok(values)
}

pub(super) fn environment_value<'a>(values: &'a [(String, String)], name: &str) -> Option<&'a str> {
    values
        .iter()
        .find_map(|(key, value)| (key == name).then_some(value.as_str()))
}

pub(super) fn required_value_default(
    prompt: &RequiredValuePrompt,
    environment: &[(String, String)],
) -> Option<String> {
    prompt
        .default
        .clone()
        .or_else(|| match prompt.env.as_str() {
            // Trust the NAS's own /24 unless the operator narrows it in .env.
            "VONK_MANAGEMENT_CIDRS" => {
                let address = environment_value(environment, "NAS_LAN_IP")?
                    .parse::<Ipv4Addr>()
                    .ok()?;
                let [a, b, c, _] = address.octets();
                Some(format!("{a}.{b}.{c}.0/24"))
            }
            _ => None,
        })
}

pub(super) fn compose_profile_enabled(value: &str, profile: &str) -> bool {
    value
        .split(',')
        .map(str::trim)
        .any(|member| member == profile)
}

pub(super) fn with_compose_profile(current: &str, hermes: &HermesPrompt, enabled: bool) -> String {
    let mut profiles = current
        .split(',')
        .map(str::trim)
        .filter(|profile| {
            !profile.is_empty()
                && *profile != hermes.enabled_value
                && *profile != hermes.disabled_value
        })
        .map(str::to_owned)
        .collect::<Vec<_>>();
    if enabled {
        profiles.push(hermes.enabled_value.clone());
    }
    if profiles.is_empty() {
        hermes.disabled_value.clone()
    } else {
        profiles.join(",")
    }
}

pub(super) fn set_environment_value(values: &mut Vec<(String, String)>, name: &str, value: String) {
    if let Some((_, existing)) = values.iter_mut().find(|(key, _)| key == name) {
        *existing = value;
    } else {
        values.push((name.to_owned(), value));
    }
}

pub(super) fn render_owned_environment(values: &[(String, String)]) -> Result<String, SetupError> {
    let borrowed = values
        .iter()
        .map(|(key, value)| (key, value.clone()))
        .collect::<Vec<_>>();
    render_environment(&borrowed)
}

pub(super) fn render_environment(values: &[(&String, String)]) -> Result<String, SetupError> {
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
