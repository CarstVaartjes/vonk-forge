#![cfg(test)]
use super::*;

fn vectors() -> Value {
    serde_json::from_str(include_str!(
            "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/run-inspect-target-requests-v1.json"
        ))
        .unwrap()
}

fn accepted(document: &Value) -> Result<HostRuntimeRequest, String> {
    let request: HostRuntimeRequest =
        serde_json::from_value(document.clone()).map_err(|error| error.to_string())?;
    request.validate().map_err(|rule| rule.to_string())?;
    Ok(request)
}

#[test]
fn every_valid_target_round_trips_to_the_python_canonical_bytes() {
    let vectors = vectors();
    let valid = vectors["valid"].as_array().unwrap();
    assert_eq!(valid.len(), 8);
    for vector in valid {
        let name = vector["name"].as_str().unwrap();
        let document = &vector["request"];
        let request = accepted(document).unwrap_or_else(|error| panic!("{name}: {error}"));
        assert_eq!(
            canonical_json(&request).unwrap(),
            canonical_json(document).unwrap(),
            "{name}"
        );
    }
}

#[test]
fn every_valid_shape_is_the_variant_its_name_says() {
    let vectors = vectors();
    for vector in vectors["valid"].as_array().unwrap() {
        let name = vector["name"].as_str().unwrap();
        let request = accepted(&vector["request"]).unwrap();
        let target = request.inspect_target.as_ref().unwrap();
        let matches = match (name, target) {
            ("installation-in-use", RunInspectTarget::InstallationInUseTarget(_))
            | ("run-name", RunInspectTarget::RunNameTarget(_))
            | ("job-identity", RunInspectTarget::JobIdentityTarget(_))
            | ("runtime-identity", RunInspectTarget::RuntimeIdentityTarget(_))
            | (
                "container-run" | "container-run bound to its generation",
                RunInspectTarget::ContainerRunTarget(_),
            ) => true,
            ("stop-identity", RunInspectTarget::StopIdentityTarget(stop)) => {
                !stop.cancel_pending_start
            }
            ("cancel-stop-identity", RunInspectTarget::StopIdentityTarget(stop)) => {
                stop.cancel_pending_start
            }
            _ => false,
        };
        assert!(matches, "{name}");
    }
}

#[test]
fn every_refused_shape_is_refused() {
    let vectors = vectors();
    let invalid = vectors["invalid"].as_array().unwrap();
    assert!(!invalid.is_empty(), "{invalid:?}");
    for vector in invalid {
        let name = vector["name"].as_str().unwrap();
        assert!(accepted(&vector["request"]).is_err(), "{name}");
    }
}
