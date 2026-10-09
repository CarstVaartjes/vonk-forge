use super::*;

#[test]
fn damaged_independent_docker_history_does_not_block_target_cleanup() {
    // Wrong implementation: global Docker history gates unrelated authorized
    // cleanup, or deleting an ambiguous sibling is used to unblock it.
    #[derive(Clone)]
    struct IndependentHistory {
        installation: String,
        cache: String,
        calls: Arc<Mutex<Vec<Vec<String>>>>,
        neighbor: Arc<Mutex<bool>>,
    }
    impl CommandRunner for IndependentHistory {
        fn run(&self, _: &Path, arguments: &[String]) -> Result<CommandOutput, String> {
            self.calls.lock().unwrap().push(arguments.to_vec());
            if arguments.first().map(String::as_str) == Some("rm") {
                *self.neighbor.lock().unwrap() = false;
            }
            let target = format!("label=ai.vonkforge.installation-id={}", self.installation);
            let volume = format!("volume={}", self.cache);
            let scoped = arguments
                .windows(2)
                .any(|pair| pair == ["--filter", &target] || pair == ["--filter", &volume]);
            Ok(CommandOutput {
                success: true,
                stdout: if scoped {
                    Vec::new()
                } else {
                    format!("{}\texited\tvonk-{}\ttrue\t\n", "a".repeat(64), RUN_ID).into_bytes()
                },
                stderr: Vec::new(),
                exit_code: Some(0),
            })
        }
    }
    let (_temp, roots, identity, runtime_cache, shared_cache) = helper_reconciliation_fixture();
    let independent = roots.data.join("independent-container-effect");
    fs::write(&independent, b"unproven independent effect").unwrap();
    let calls = Arc::new(Mutex::new(Vec::new()));
    let neighbor = Arc::new(Mutex::new(true));
    let executor = OperationExecutor::new(
        roots,
        &[0; 32],
        IndependentHistory {
            installation: identity.installation_id.to_string(),
            cache: runtime_cache.display().to_string(),
            calls: Arc::clone(&calls),
            neighbor: Arc::clone(&neighbor),
        },
        None,
    )
    .unwrap();
    for _ in 0..2 {
        executor.runtime_reconcile_installation(&identity).unwrap();
        assert!(!runtime_cache.exists());
        assert!(*neighbor.lock().unwrap(), "unbound stopped neighbor was removed");
        assert_eq!(
            fs::read(&independent).unwrap(),
            b"unproven independent effect"
        );
        assert_eq!(fs::read(&shared_cache).unwrap(), b"shared model cache");
    }
    assert!(
        calls
            .lock()
            .unwrap()
            .iter()
            .all(|call| call[0..2] == ["container", "ls"])
    );
}

#[test]
fn damaged_target_labels_cannot_hide_an_existing_cache_consumer() {
    // Wrong implementation: a label-only query infers absence after target
    // label damage and removes bytes still mounted by that container.
    #[derive(Clone)]
    struct CacheConsumer(bool);
    impl CommandRunner for CacheConsumer {
        fn run(&self, _: &Path, args: &[String]) -> Result<CommandOutput, String> {
            let cache_query = args.iter().any(|arg| arg.starts_with("volume="));
            Ok(CommandOutput {
                success: true,
                stdout: if self.0 && cache_query {
                    b"malformed target observation".to_vec()
                } else {
                    Vec::new()
                },
                stderr: Vec::new(),
                exit_code: Some(0),
            })
        }
    }
    let (_temp, roots, identity, cache, shared) = helper_reconciliation_fixture();
    let unknown =
        OperationExecutor::new(roots.clone(), &[0; 32], CacheConsumer(true), None).unwrap();
    assert!(unknown.runtime_reconcile_installation(&identity).is_err());
    assert!(cache.exists());
    assert_eq!(fs::read(&shared).unwrap(), b"shared model cache");
    let fresh = OperationExecutor::new(roots, &[0; 32], CacheConsumer(false), None).unwrap();
    fresh.runtime_reconcile_installation(&identity).unwrap();
    assert!(!cache.exists());
    assert_eq!(fs::read(shared).unwrap(), b"shared model cache");
}
