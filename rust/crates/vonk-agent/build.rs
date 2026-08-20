use std::env;

const DEVELOPMENT_BUILD_DIGEST: &str =
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn main() {
    println!("cargo:rerun-if-env-changed=VONK_AGENT_BUILD_DIGEST");
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
}
