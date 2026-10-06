//! An upstream Compose file, read as the untrusted document it is.
//!
//! Reason this is not a contract type: the file belongs to the recipe's own
//! build source, not to the Vonk wire protocol. The source policy must judge
//! whatever shape a creator wrote (including malformed ones) and fail closed,
//! so it reads the YAML structurally instead of deserializing a declared model.
//! Every question the policy asks is a method here; the YAML value type never
//! leaves this module.
use serde_yaml::{Mapping, Value};

pub struct ComposeDocument(Value);

pub struct ComposeService<'a>(Option<&'a Mapping>);

/// One volume entry: its source and whether it was declared as a bind mount.
pub struct ComposeVolume {
    pub source: String,
    pub explicit_bind: bool,
}

impl ComposeDocument {
    pub fn parse(payload: &[u8]) -> Option<Self> {
        serde_yaml::from_slice::<Value>(payload).ok().map(Self)
    }

    /// The services mapping, or `None` when the document has none.
    pub fn services(&self) -> Option<Vec<ComposeService<'_>>> {
        let services = get(self.0.as_mapping(), "services")?.as_mapping()?;
        Some(
            services
                .values()
                .map(|service| ComposeService(service.as_mapping()))
                .collect(),
        )
    }
}

impl ComposeService<'_> {
    pub fn is_mapping(&self) -> bool {
        self.0.is_some()
    }

    pub fn privileged(&self) -> bool {
        get(self.0, "privileged").and_then(Value::as_bool) == Some(true)
    }

    /// How many of the namespace keys name the host namespace.
    pub fn host_namespaces(&self) -> usize {
        ["network_mode", "pid", "ipc", "uts", "userns_mode"]
            .into_iter()
            .filter(|key| get(self.0, key).and_then(Value::as_str) == Some("host"))
            .count()
    }

    pub fn adds_capabilities(&self) -> bool {
        nonempty_sequence(get(self.0, "cap_add"))
    }

    pub fn requests_devices(&self) -> bool {
        nonempty_sequence(get(self.0, "devices"))
    }

    pub fn unconfined_security_option(&self) -> bool {
        get(self.0, "security_opt")
            .and_then(Value::as_sequence)
            .map(|items| items.iter().filter_map(Value::as_str).collect::<Vec<_>>())
            .unwrap_or_default()
            .iter()
            .any(|value| value.to_ascii_lowercase().contains("unconfined"))
    }

    pub fn volumes_malformed(&self) -> bool {
        get(self.0, "volumes").is_some_and(|value| !value.is_sequence())
    }

    pub fn volumes(&self) -> Vec<ComposeVolume> {
        get(self.0, "volumes")
            .and_then(Value::as_sequence)
            .map(|items| {
                items
                    .iter()
                    .filter_map(|item| match item {
                        Value::String(short) => {
                            short.split(':').next().map(|source| ComposeVolume {
                                source: source.to_owned(),
                                explicit_bind: false,
                            })
                        }
                        Value::Mapping(long) => get(Some(long), "source")
                            .and_then(Value::as_str)
                            .map(|source| ComposeVolume {
                                source: source.to_owned(),
                                explicit_bind: get(Some(long), "type").and_then(Value::as_str)
                                    == Some("bind"),
                            }),
                        _ => None,
                    })
                    .collect()
            })
            .unwrap_or_default()
    }
}

fn get<'a>(mapping: Option<&'a Mapping>, key: &str) -> Option<&'a Value> {
    mapping?.get(Value::String(key.to_owned()))
}

fn nonempty_sequence(value: Option<&Value>) -> bool {
    value
        .and_then(Value::as_sequence)
        .is_some_and(|items| !items.is_empty())
}
