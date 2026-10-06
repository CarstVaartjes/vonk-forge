#![forbid(unsafe_code)]

use std::collections::BTreeMap;

use vonk_agent::source_policy::inspect_build_source;
use vonk_agent_protocol::generated::SourcePolicyCode;

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
            SourcePolicyCode::DockerfileBaseUnpinned,
        ),
        (
            &format!("FROM ubuntu@sha256:{}\nUSER 10001\n", "0".repeat(64)),
            SourcePolicyCode::DockerfileBasePlaceholder,
        ),
        (
            &format!(
                "FROM ubuntu@sha256:{}\nADD https://evil.invalid/x /x\nUSER 10001\n",
                "a".repeat(64)
            ),
            SourcePolicyCode::DockerfileAddForbidden,
        ),
        (
            &format!(
                "FROM ubuntu@sha256:{}\nRUN --mount=type=ssh true\nUSER 10001\n",
                "a".repeat(64)
            ),
            SourcePolicyCode::DockerfileSecretMount,
        ),
        (
            &format!(
                "FROM ubuntu@sha256:{}\nCOPY ../secret /x\nUSER 10001\n",
                "a".repeat(64)
            ),
            SourcePolicyCode::DockerfileCopyPath,
        ),
        (
            &format!(
                "FROM ubuntu@sha256:{}\nCOPY /etc/passwd /x\nUSER 10001\n",
                "a".repeat(64)
            ),
            SourcePolicyCode::DockerfileCopyPath,
        ),
        (
            &format!("FROM ubuntu@sha256:{}\nUSER root\n", "a".repeat(64)),
            SourcePolicyCode::DockerfileRootUser,
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
    assert!(codes.contains(&SourcePolicyCode::ComposePrivileged));
    assert!(codes.contains(&SourcePolicyCode::ComposeHostNamespace));
    assert!(codes.contains(&SourcePolicyCode::ComposeHostBind));
}

#[test]
fn malformed_compose_fails_closed() {
    let mut source = files(&format!(
        "FROM ghcr.io/vonkforge/vllm@sha256:{}\nUSER 10001\n",
        "a".repeat(64)
    ));
    source.insert("docker-compose.yml".to_owned(), b"services: [".to_vec());

    let report = inspect_build_source(&source, "Dockerfile");
    assert_eq!(
        report.findings.last().unwrap().code,
        SourcePolicyCode::ComposeInvalid
    );
}

#[test]
fn a_container_engine_socket_is_a_host_bind_finding_even_behind_a_named_volume() {
    // Wrong implementation: only a host path source was checked, so mounting the
    // socket into the container through a named volume passed the recheck.
    let mut source = files(&format!(
        "FROM ghcr.io/vonkforge/vllm@sha256:{}\nUSER 10001\n",
        "a".repeat(64)
    ));
    source.insert(
        "compose.yaml".to_owned(),
        b"services:\n  model:\n    volumes:\n      - engine:/var/run/podman.sock\n".to_vec(),
    );

    let report = inspect_build_source(&source, "Dockerfile");

    assert_eq!(
        report
            .findings
            .iter()
            .map(|item| item.code)
            .collect::<Vec<_>>(),
        [SourcePolicyCode::ComposeHostBind]
    );
}

#[test]
fn the_copy_rules_name_the_contract_codes_the_controller_names() {
    for (copy, code) in [
        ("COPY [\"a\", ", SourcePolicyCode::DockerfileCopyInvalid),
        ("COPY ~/secret /x", SourcePolicyCode::DockerfileCopyPath),
        ("COPY $CONTEXT /x", SourcePolicyCode::DockerfileCopyPath),
        (
            "COPY --from=ghcr.io/vonkforge/tools:latest /bin/tool /x",
            SourcePolicyCode::DockerfileCopyBaseUnpinned,
        ),
        (
            "COPY --from=ghcr.io/vonkforge/tools@sha256:0000000000000000000000000000000000000000000000000000000000000000 /bin/tool /x",
            SourcePolicyCode::DockerfileCopyBasePlaceholder,
        ),
    ] {
        let report = inspect_build_source(
            &files(&format!(
                "FROM ghcr.io/vonkforge/vllm@sha256:{}\n{copy}\nUSER 10001\n",
                "a".repeat(64)
            )),
            "Dockerfile",
        );
        assert!(
            report.findings.iter().any(|item| item.code == code),
            "{copy}: {:?}",
            report.findings
        );
    }
}
