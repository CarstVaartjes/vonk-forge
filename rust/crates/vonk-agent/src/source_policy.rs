//! Agent-side, fail-closed source policy recheck for recipe build inputs.

use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;
use serde_yaml::{Mapping, Value};

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SourceFinding {
    pub code: &'static str,
    pub path: String,
    pub line: Option<usize>,
    pub detail: &'static str,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SourcePolicyReport {
    pub passed: bool,
    pub dockerfile: String,
    pub findings: Vec<SourceFinding>,
}

pub fn inspect_build_source(
    files: &BTreeMap<String, Vec<u8>>,
    dockerfile: &str,
) -> SourcePolicyReport {
    let mut findings = Vec::new();
    match files.get(dockerfile) {
        Some(payload) => inspect_dockerfile(dockerfile, payload, &mut findings),
        None => findings.push(finding(
            "dockerfile.missing",
            dockerfile,
            None,
            "recipe Dockerfile is missing from the source bundle",
        )),
    }
    for (path, payload) in files {
        let name = path.rsplit('/').next().unwrap_or(path).to_ascii_lowercase();
        if matches!(
            name.as_str(),
            "compose.yml" | "compose.yaml" | "docker-compose.yml" | "docker-compose.yaml"
        ) {
            inspect_compose(path, payload, &mut findings);
        }
    }
    findings.sort_by(|left, right| {
        (&left.path, left.line.unwrap_or(0), left.code).cmp(&(
            &right.path,
            right.line.unwrap_or(0),
            right.code,
        ))
    });
    SourcePolicyReport {
        passed: findings.is_empty(),
        dockerfile: dockerfile.to_owned(),
        findings,
    }
}

pub fn dockerfile_base_images(
    files: &BTreeMap<String, Vec<u8>>,
    dockerfile: &str,
) -> Option<Vec<String>> {
    let text = std::str::from_utf8(files.get(dockerfile)?).ok()?;
    let mut seen = BTreeSet::new();
    let mut images = Vec::new();
    for (_line, instruction, argument) in dockerfile_instructions(text) {
        if !instruction.eq_ignore_ascii_case("FROM") {
            continue;
        }
        let reference = argument
            .split_ascii_whitespace()
            .find(|value| !value.starts_with("--platform="))?;
        if reference == "scratch" {
            continue;
        }
        if !pinned_image(reference) {
            return None;
        }
        if seen.insert(reference.to_owned()) {
            images.push(reference.to_owned());
        }
    }
    Some(images)
}

fn inspect_dockerfile(path: &str, payload: &[u8], findings: &mut Vec<SourceFinding>) {
    let Ok(text) = std::str::from_utf8(payload) else {
        findings.push(finding(
            "dockerfile.invalid_utf8",
            path,
            None,
            "Dockerfile must be UTF-8",
        ));
        return;
    };
    let mut aliases = BTreeSet::new();
    let mut final_user: Option<(String, usize)> = None;
    let mut saw_from = false;
    for (line, instruction, argument) in dockerfile_instructions(text) {
        if argument.contains("<<") {
            findings.push(finding(
                "dockerfile.heredoc_forbidden",
                path,
                Some(line),
                "Dockerfile heredocs are not accepted",
            ));
        }
        match instruction.to_ascii_uppercase().as_str() {
            "FROM" => {
                saw_from = true;
                final_user = None;
                let tokens = argument
                    .split_ascii_whitespace()
                    .filter(|value| !value.starts_with("--platform="))
                    .collect::<Vec<_>>();
                let base = tokens.first().copied().unwrap_or_default();
                if placeholder_image(base) {
                    findings.push(finding(
                        "dockerfile.base_placeholder",
                        path,
                        Some(line),
                        "replace the all-zero base-image placeholder with a verified linux/arm64 digest",
                    ));
                } else if base != "scratch" && !pinned_image(base) {
                    findings.push(finding(
                        "dockerfile.base_unpinned",
                        path,
                        Some(line),
                        "every base image must be pinned by sha256 digest",
                    ));
                }
                if tokens.len() >= 3 && tokens[tokens.len() - 2].eq_ignore_ascii_case("as") {
                    aliases.insert(tokens[tokens.len() - 1].to_owned());
                }
            }
            "USER" => final_user = Some((argument.trim().to_owned(), line)),
            "ADD" => findings.push(finding(
                "dockerfile.add_forbidden",
                path,
                Some(line),
                "ADD is forbidden; use bounded local COPY",
            )),
            "ONBUILD" => findings.push(finding(
                "dockerfile.onbuild_forbidden",
                path,
                Some(line),
                "ONBUILD may hide deferred build instructions",
            )),
            "RUN" => {
                let compact = argument.to_ascii_lowercase().replace(' ', "");
                if compact.contains("--mount=type=secret") || compact.contains("--mount=type=ssh") {
                    findings.push(finding(
                        "dockerfile.secret_mount",
                        path,
                        Some(line),
                        "secret and SSH build mounts are forbidden",
                    ));
                }
                if compact.contains("--network=host") || compact.contains("--security=insecure") {
                    findings.push(finding(
                        "dockerfile.build_privilege",
                        path,
                        Some(line),
                        "host networking and insecure build execution are forbidden",
                    ));
                }
            }
            "COPY" => inspect_copy(path, line, &argument, &aliases, findings),
            _ => {}
        }
    }
    if !saw_from {
        findings.push(finding(
            "dockerfile.from_missing",
            path,
            None,
            "Dockerfile must declare a base stage",
        ));
    }
    if final_user
        .as_ref()
        .is_none_or(|(user, _)| !non_root_user(user))
    {
        findings.push(finding(
            "dockerfile.root_user",
            path,
            final_user.map(|(_, line)| line),
            "the final image stage must select an explicit numeric non-root user",
        ));
    }
}

fn inspect_copy(
    path: &str,
    line: usize,
    argument: &str,
    aliases: &BTreeSet<String>,
    findings: &mut Vec<SourceFinding>,
) {
    let mut tokens = argument.split_ascii_whitespace().collect::<Vec<_>>();
    let copies_from_stage = tokens.iter().any(|value| value.starts_with("--from="));
    if let Some(from) = tokens
        .iter()
        .find_map(|value| value.strip_prefix("--from="))
        && !aliases.contains(from)
        && placeholder_image(from)
    {
        findings.push(finding(
            "dockerfile.copy_stage_placeholder",
            path,
            Some(line),
            "replace the all-zero external COPY placeholder with a verified digest",
        ));
    } else if let Some(from) = tokens
        .iter()
        .find_map(|value| value.strip_prefix("--from="))
        && !aliases.contains(from)
        && !pinned_image(from)
    {
        findings.push(finding(
            "dockerfile.copy_stage_unpinned",
            path,
            Some(line),
            "COPY --from must reference a declared stage or pinned image",
        ));
    }
    tokens.retain(|value| !value.starts_with("--"));
    let sources = if argument.trim_start().starts_with('[') {
        serde_json::from_str::<Vec<String>>(argument)
            .ok()
            .map(|items| {
                let count = items.len().saturating_sub(1);
                items.into_iter().take(count).collect()
            })
            .unwrap_or_default()
    } else {
        let count = tokens.len().saturating_sub(1);
        tokens
            .into_iter()
            .take(count)
            .map(str::to_owned)
            .collect::<Vec<_>>()
    };
    if sources.is_empty()
        || sources.iter().any(|value| {
            (value.starts_with('/') && !copies_from_stage)
                || value.split('/').any(|part| part == "..")
        })
    {
        findings.push(finding(
            "dockerfile.copy_escape",
            path,
            Some(line),
            "COPY sources must stay inside the canonical build context",
        ));
    }
}

fn inspect_compose(path: &str, payload: &[u8], findings: &mut Vec<SourceFinding>) {
    let Ok(document) = serde_yaml::from_slice::<Value>(payload) else {
        findings.push(finding(
            "compose.invalid",
            path,
            None,
            "Compose document is invalid",
        ));
        return;
    };
    let Some(services) = mapping_get(document.as_mapping(), "services").and_then(Value::as_mapping)
    else {
        findings.push(finding(
            "compose.invalid",
            path,
            None,
            "Compose document must contain a services mapping",
        ));
        return;
    };
    for service in services.values() {
        let Some(service) = service.as_mapping() else {
            findings.push(finding(
                "compose.service_invalid",
                path,
                None,
                "Compose service must be a mapping",
            ));
            continue;
        };
        if mapping_get(Some(service), "privileged").and_then(Value::as_bool) == Some(true) {
            findings.push(finding(
                "compose.privileged",
                path,
                None,
                "privileged Compose services are forbidden",
            ));
        }
        for key in ["network_mode", "pid", "ipc", "uts", "userns_mode"] {
            if mapping_get(Some(service), key).and_then(Value::as_str) == Some("host") {
                findings.push(finding(
                    "compose.host_namespace",
                    path,
                    None,
                    "host namespaces are forbidden",
                ));
            }
        }
        if nonempty_sequence(mapping_get(Some(service), "cap_add")) {
            findings.push(finding(
                "compose.capabilities",
                path,
                None,
                "added Linux capabilities are forbidden",
            ));
        }
        if nonempty_sequence(mapping_get(Some(service), "devices")) {
            findings.push(finding(
                "compose.devices",
                path,
                None,
                "Compose build metadata may not request host devices",
            ));
        }
        if sequence_strings(mapping_get(Some(service), "security_opt"))
            .iter()
            .any(|value| value.to_ascii_lowercase().contains("unconfined"))
        {
            findings.push(finding(
                "compose.unconfined",
                path,
                None,
                "unconfined security profiles are forbidden",
            ));
        }
        let volumes = mapping_get(Some(service), "volumes");
        if volumes.is_some_and(|value| !value.is_sequence()) {
            findings.push(finding(
                "compose.volumes_invalid",
                path,
                None,
                "Compose volumes must be a sequence",
            ));
        }
        for (source, explicit_bind) in volume_sources(volumes) {
            if source.contains("docker.sock") || source.contains("podman.sock") {
                findings.push(finding(
                    "compose.container_socket",
                    path,
                    None,
                    "container runtime sockets are forbidden",
                ));
            }
            if explicit_bind
                || source.starts_with('/')
                || source.starts_with("./")
                || source.starts_with("../")
                || source == "."
                || source == ".."
                || source.starts_with('~')
            {
                findings.push(finding(
                    "compose.host_bind",
                    path,
                    None,
                    "host bind mounts are forbidden",
                ));
            }
        }
    }
}

fn mapping_get<'a>(mapping: Option<&'a Mapping>, key: &str) -> Option<&'a Value> {
    mapping?.get(Value::String(key.to_owned()))
}

fn nonempty_sequence(value: Option<&Value>) -> bool {
    value
        .and_then(Value::as_sequence)
        .is_some_and(|items| !items.is_empty())
}

fn sequence_strings(value: Option<&Value>) -> Vec<&str> {
    value
        .and_then(Value::as_sequence)
        .map(|items| items.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default()
}

fn volume_sources(value: Option<&Value>) -> Vec<(String, bool)> {
    value
        .and_then(Value::as_sequence)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| match item {
                    Value::String(short) => short
                        .split(':')
                        .next()
                        .map(|source| (source.to_owned(), false)),
                    Value::Mapping(long) => mapping_get(Some(long), "source")
                        .and_then(Value::as_str)
                        .map(|source| {
                            let explicit_bind = mapping_get(Some(long), "type")
                                .and_then(Value::as_str)
                                == Some("bind");
                            (source.to_owned(), explicit_bind)
                        }),
                    _ => None,
                })
                .collect()
        })
        .unwrap_or_default()
}

fn dockerfile_instructions(text: &str) -> Vec<(usize, String, String)> {
    let mut result = Vec::new();
    let mut logical = String::new();
    let mut start = 0;
    for (offset, raw) in text.lines().enumerate() {
        let line = offset + 1;
        let trimmed = raw.trim();
        if logical.is_empty() && (trimmed.is_empty() || trimmed.starts_with('#')) {
            continue;
        }
        if logical.is_empty() {
            start = line;
        }
        if let Some(value) = trimmed.strip_suffix('\\') {
            logical.push_str(value.trim_end());
            logical.push(' ');
            continue;
        }
        logical.push_str(trimmed);
        let (instruction, argument) = logical
            .split_once(char::is_whitespace)
            .unwrap_or((logical.as_str(), ""));
        result.push((start, instruction.to_owned(), argument.trim().to_owned()));
        logical.clear();
    }
    if !logical.is_empty() {
        let (instruction, argument) = logical
            .split_once(char::is_whitespace)
            .unwrap_or((logical.as_str(), ""));
        result.push((start, instruction.to_owned(), argument.trim().to_owned()));
    }
    result
}

fn pinned_image(value: &str) -> bool {
    let Some((name, digest)) = value.rsplit_once("@sha256:") else {
        return false;
    };
    !name.is_empty()
        && digest.len() == 64
        && digest
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn placeholder_image(value: &str) -> bool {
    value
        .rsplit_once("@sha256:")
        .is_some_and(|(name, digest)| !name.is_empty() && digest == "0".repeat(64))
}

fn non_root_user(value: &str) -> bool {
    let mut parts = value.split(':');
    let valid = |part: &str| {
        !part.is_empty() && part.bytes().all(|byte| byte.is_ascii_digit()) && !part.starts_with('0')
    };
    valid(parts.next().unwrap_or_default())
        && parts.next().is_none_or(valid)
        && parts.next().is_none()
}

fn finding(
    code: &'static str,
    path: &str,
    line: Option<usize>,
    detail: &'static str,
) -> SourceFinding {
    SourceFinding {
        code,
        path: path.to_owned(),
        line,
        detail,
    }
}
