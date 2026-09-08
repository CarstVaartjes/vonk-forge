//! Complete unrelated release graph members for isolated setup security fixtures.
//! The publisher-to-Rust probe separately verifies the actual production graph.
use serde_json::{Value, json};
use vonk_agent_protocol::generated::InstallerCandidateRelease;

pub fn candidate_release(mut document: Value) -> InstallerCandidateRelease {
    let prefix = format!(
        "artifacts/{}/releases/{}",
        document["channel"].as_str().unwrap(),
        document["generation"].as_str().unwrap()
    );
    let object = |path: String| json!({"path": path, "sha256": "c".repeat(64), "size": 1});
    for platform in ["linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64"] {
        document["artifacts"][format!("nas-setup-{platform}")] =
            object(format!("{prefix}/nas/current/{platform}/vonk-nas-setup"));
    }
    document["artifacts"]["nas-payload"] = object(format!("{prefix}/nas/current/payload.json"));
    document["artifacts"]["agent-package-signature-linux-arm64"] = object(format!(
        "{prefix}/spark/current/linux-arm64/vonk-forge-agent.deb.host.sig"
    ));
    document["bootstraps"] = json!({
        "nas": object(format!("{prefix}/bootstraps/nas")),
        "spark": object(format!("{prefix}/bootstraps/spark")),
    });
    document["images"] = ["api", "worker", "hermes", "litellm"]
        .into_iter()
        .map(|role| {
            (
                role.to_owned(),
                json!(format!(
                    "example.test/{role}:v1.0.0@sha256:{}",
                    "e".repeat(64)
                )),
            )
        })
        .collect();
    serde_json::from_value(document).expect("setup fixture must be a complete canonical release")
}
