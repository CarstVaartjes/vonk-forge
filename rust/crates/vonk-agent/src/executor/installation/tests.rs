#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn recipe_uninstall_with_optional_model_cleanup_is_executed() {
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

    let result = executor.execute(&claim, lease_deadline, cancellation).await;

    assert_eq!(result.state(), AgentResultState::Succeeded);
    assert!(matches!(
        result,
        ExecutionResult::Done(OutcomeDoneResult::RecipeUninstallResult(_))
    ));
    assert!(!installation.exists());
    // Model cleanup frees the store's copy once no installation links it.
    assert!(stored_model.iter().all(|object| !object.exists()));
    assert!(another_model.exists());
}
