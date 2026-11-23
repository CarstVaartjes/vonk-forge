#![expect(clippy::expect_used, reason = "build script: a panic fails the build")]
use std::env;

const DEVELOPMENT_BUILD_DIGEST: &str =
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
fn main() {
    println!("cargo:rerun-if-env-changed=VONK_AGENT_BUILD_DIGEST");
    println!("cargo:rerun-if-env-changed=VONK_AGENT_SEMANTIC_VERSION");
    let digest =
        env::var("VONK_AGENT_BUILD_DIGEST").unwrap_or_else(|_| DEVELOPMENT_BUILD_DIGEST.to_owned());
    assert!(
        digest.len() == 71
            && digest.starts_with("sha256:")
            && digest[7..]
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()),
        "VONK_AGENT_BUILD_DIGEST must be a canonical SHA-256 digest"
    );
    println!("cargo:rustc-env=VONK_AGENT_BUILD_DIGEST={digest}");
    let semantic_version = env::var("VONK_AGENT_SEMANTIC_VERSION")
        .unwrap_or_else(|_| env::var("CARGO_PKG_VERSION").expect("Cargo package version exists"));
    let parts = semantic_version.split('.').collect::<Vec<_>>();
    assert!(
        parts.len() == 3
            && parts.iter().all(|part| {
                !part.is_empty()
                    && part.bytes().all(|byte| byte.is_ascii_digit())
                    && (*part == "0" || !part.starts_with('0'))
            }),
        "VONK_AGENT_SEMANTIC_VERSION must be canonical SemVer"
    );
    println!("cargo:rustc-env=VONK_AGENT_SEMANTIC_VERSION={semantic_version}");
}
