#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn uninstall_retains_objects_until_runtime_confirmation_then_resumes_local_removal() {
    uninstall_after_observation(false).await;
}

#[tokio::test]
async fn damaged_uninstall_observation_preserves_bytes_and_allows_a_fresh_request() {
    uninstall_after_observation(true).await;
}

async fn uninstall_after_observation(damage: bool) {
    let data = tempdir().unwrap();
    let runtime_root = tempdir().unwrap();
    let installation_id = "00000000-0000-4000-8000-000000000001";
    let plan: Value = serde_json::from_str(include_str!(
        "../../../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    let model_content_sha256 = plan["artifacts"][0]["model"]["content_sha256"]
        .as_str()
        .unwrap()
        .to_owned();
    let installation = data.path().join("installations").join(installation_id);
    fs::create_dir_all(&installation).unwrap();
    fs::write(
        installation.join("spec.json"),
        serde_json::to_vec(&plan).unwrap(),
    )
    .unwrap();
    let recipe_content_sha256 = plan["identity"]["recipe_revision_sha256"].as_str().unwrap();
    fs::write(
        installation.join("recipe-content.sha256"),
        recipe_content_sha256,
    )
    .unwrap();
    // The Spark's shared store holds the model's files and another model's.
    let store = data.path().join("distribution/models");
    fs::create_dir_all(&store).unwrap();
    let stored_model: Vec<_> = plan["artifacts"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|artifact| artifact["model"]["content_sha256"] == model_content_sha256)
        .map(|artifact| store.join(artifact["sha256"].as_str().unwrap()))
        .collect();
    assert!(!stored_model.is_empty());
    for object in &stored_model {
        fs::write(object, b"model bytes").unwrap();
    }
    let another_model = store.join("e".repeat(64));
    fs::write(&another_model, b"another model").unwrap();

    let claim = AgentClaim {
        observation_budget_seconds: 3600,
        deadline: (Utc::now() + ChronoDuration::seconds(20))
            .with_timezone(&FixedOffset::east_opt(0).unwrap()),
        fence: Uuid::new_v4(),
        operation: AgentOperation::RecipeUninstall,
        payload: serde_json::from_value(serde_json::json!({
            "installation_id": installation_id,
            "recipe_content_sha256": recipe_content_sha256,
            "cleanup_model_content_sha256": model_content_sha256,
            "plan_digest": "b".repeat(64),
        }))
        .unwrap(),
    };
    let client = AgentHttpClient::for_http_test("http://127.0.0.1/", NODE_ID);
    let runner = NoProcess;
    let executor = RecipeExecutor {
        client: &client,
        runtime: OciRuntime {
            runner: &runner,
            data_root: data.path(),
        },
        runtime_root: runtime_root.path(),
    };
    let (_lease_sender, lease_deadline) = tokio::sync::watch::channel(claim.deadline);
    let (_cancel_sender, cancellation) = tokio::sync::watch::channel(false);

    if damage {
        fs::write(installation.join("spec.json"), b"{}").unwrap();
        let unknown = executor
            .execute(&claim, lease_deadline.clone(), cancellation.clone())
            .await;
        assert_eq!(unknown.state(), AgentResultState::Observing);
        assert!(installation.exists());
        assert!(stored_model.iter().all(|object| object.exists()));
    }
    let mut fresh = claim.clone();
    fresh.fence = Uuid::new_v4();
    fresh.payload = serde_json::from_value(serde_json::json!({
        "installation_id": installation_id,
        "recipe_content_sha256": recipe_content_sha256,
        "cleanup_model_content_sha256": model_content_sha256,
        "plan_digest": "b".repeat(64),
        "compiled_execution_plan": plan,
    }))
    .unwrap();
    let result = executor.execute(&fresh, lease_deadline, cancellation).await;

    assert!(matches!(result, ExecutionResult::Unknown(_)));
    assert!(installation.exists());
    assert!(stored_model.iter().all(|object| object.exists()));
    // The fresh accepted typed plan repairs discovery before runtime observation.
    // Damaged bookkeeping must neither lose bytes nor poison the next request.
    let repaired = executor.runtime.load_spec(installation_id).unwrap();
    assert_eq!(repaired.identity.recipe_revision_sha256, recipe_content_sha256);
    // At the local producer/store seam, a confirmed privileged cleanup can
    // finish the exact checkpoint even if interruption removed identifying files.
    let identity = RecipeReconciliationIdentity {
        installation_id: Uuid::parse_str(installation_id).unwrap(),
        plan_digest: "b".repeat(64),
    };
    fs::remove_file(installation.join("spec.json")).unwrap();
    fs::remove_file(installation.join("recipe-content.sha256")).unwrap();
    executor.runtime.prepare_reconciliation(&identity).unwrap();
    assert!(
        executor
            .runtime
            .finalize_reconciliation(&identity)
            .unwrap()
            .complete
    );
    executor
        .runtime
        .reclaim_unshared_model_objects(
            &stored_model
                .iter()
                .map(|path| path.file_name().unwrap().to_str().unwrap().to_owned())
                .collect::<Vec<_>>(),
        )
        .unwrap();
    assert!(!installation.exists());
    assert!(stored_model.iter().all(|object| !object.exists()));
    assert!(another_model.exists());
    // A new request supersedes cleanup history without a permanent removal gate.
    fs::create_dir_all(&installation).unwrap();
    assert!(
        !executor
            .runtime
            .prepare_reconciliation(&identity)
            .unwrap()
            .complete
    );
    assert!(
        executor
            .runtime
            .finalize_reconciliation(&identity)
            .unwrap()
            .complete
    );
}
