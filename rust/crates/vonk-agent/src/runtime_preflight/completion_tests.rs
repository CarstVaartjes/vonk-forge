use super::*;

#[test]
fn completed_preflight_cache_reopens_the_shared_fence_bound_receipt_and_stays_bounded() {
    // Wrong implementation: only build findings survive, or another
    // request's preflight receipt is adopted after process death.
    let raw =
        include_bytes!("../../../../../agent_protocol/fixtures/runtime-preflight-completion.json");
    let completion: RuntimePreflightCompletion = vonk_agent_protocol::parse_strict(raw).unwrap();
    let root = tempfile::tempdir().unwrap();
    retain_completed(root.path(), completion.fence, &completion.result).unwrap();
    let path = root
        .path()
        .join("preflight-completion")
        .join("completed.json");
    assert_eq!(fs::read(path).unwrap(), raw.strip_suffix(b"\n").unwrap());
    assert_eq!(
        observe_completed(
            root.path(),
            completion.fence,
            &completion.result.fingerprint
        )
        .unwrap(),
        Some(completion.result.clone())
    );
    assert!(
        observe_completed(
            root.path(),
            uuid::Uuid::new_v4(),
            &completion.result.fingerprint
        )
        .unwrap()
        .is_none()
    );
    assert!(
        observe_completed(root.path(), completion.fence, &"b".repeat(64))
            .unwrap()
            .is_none()
    );
    let next = uuid::Uuid::new_v4();
    retain_completed(root.path(), next, &completion.result).unwrap();
    assert!(
        observe_completed(
            root.path(),
            completion.fence,
            &completion.result.fingerprint
        )
        .unwrap()
        .is_none()
    );
    let directory = root.path().join("preflight-completion");
    assert_eq!(fs::read_dir(directory).unwrap().count(), 1);
}
