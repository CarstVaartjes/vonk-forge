#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn canonical_job_claim_prepares_without_a_serving_port() {
    let claim: Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    let request: vonk_agent_protocol::RecipeJobRunRequest =
        serde_json::from_value(claim["payload"].clone()).unwrap();
    let spec = request.compiled_execution_plan.clone();
    spec.validate().unwrap();
    let placement = super::job_placement(&spec).unwrap();
    let data = tempdir().unwrap();
    let run_id = request.run_id.to_string();
    fs::create_dir_all(data.path().join("runs").join(&run_id).join("inputs")).unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: data.path(),
    };
    let start = runtime
        .prepare_job_start(
            &spec,
            &request.installation_id.to_string(),
            &run_id,
            &placement,
            &spec,
        )
        .unwrap();
    assert!(!start.main.iter().any(|argument| argument == "--publish"));
    assert!(
        start
            .main
            .windows(2)
            .any(|pair| pair == ["--network", "none"])
    );
    let metadata = data.path().join("run-metadata").join(run_id);
    for name in ["runtime.json", "lifecycle.json"] {
        let persisted: Value =
            serde_json::from_slice(&fs::read(metadata.join(name)).unwrap()).unwrap();
        let port = if name == "runtime.json" {
            &persisted["runtime"]["placement"]["port"]
        } else {
            &persisted["placement"]["port"]
        };
        assert!(port.is_null());
    }
    let mut serving_placement = placement;
    serving_placement.port = Some(1024);
    assert!(
        runtime
            .start_arguments(
                &spec,
                &request.installation_id.to_string(),
                &request.run_id.to_string(),
                &serving_placement
            )
            .is_err()
    );
}

#[test]
fn job_run_stop_binding_keeps_parent_run_and_exact_job_target_distinct() {
    let claim: AgentClaim = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    let AgentClaimPayload::RecipeJobRunRequest(job) = &claim.payload else {
        panic!("expected canonical job-run claim");
    };
    let stop = exact_stop_plan_from_claim(&claim, &job.run_id.to_string(), true)
        .expect("accepted JobRun must produce its exact cancellation Stop");

    assert_eq!(stop.run_id, job.run_id);
    assert_eq!(stop.target_runtime_id, job.job_id);
    assert_ne!(stop.run_id, stop.target_runtime_id);
    assert_eq!(stop.run_generation, job.run_generation);
    assert_eq!(stop.installation_id, job.installation_id);
    assert_eq!(stop.mapping_id, job.mapping_id);
    assert!(stop.cancel_pending_start);

    let mut stop_claim = claim.clone();
    stop_claim.operation = AgentOperation::RecipeStop;
    stop_claim.payload = AgentClaimPayload::RecipeStopPayload(stop);
    assert!(matches!(
        RecipeOperationRequest::parse(&stop_claim),
        Ok(RecipeOperationRequest::Stop(_))
    ));
}

#[test]
fn signed_output_mappings_cover_pdf_avif_and_custom_suffixes() {
    let mappings = vec![
        RecipeJobOutputMapping {
            slot: "custom".to_owned(),
            media_type: "application/vnd.vonk.custom".to_owned(),
            extensions: vec![".vonk.bin".to_owned()],
        },
        RecipeJobOutputMapping {
            slot: "document".to_owned(),
            media_type: "application/pdf".to_owned(),
            extensions: vec![".pdf".to_owned()],
        },
        RecipeJobOutputMapping {
            slot: "fallback".to_owned(),
            media_type: "application/octet-stream".to_owned(),
            extensions: vec![".bin".to_owned()],
        },
        RecipeJobOutputMapping {
            slot: "image".to_owned(),
            media_type: "image/avif".to_owned(),
            extensions: vec![".avif".to_owned()],
        },
    ];

    assert_eq!(
        output_media_type("report.pdf", &mappings),
        Some("application/pdf")
    );
    assert_eq!(
        output_media_type("frame.avif", &mappings),
        Some("image/avif")
    );
    assert_eq!(
        output_media_type("artifact.vonk.bin", &mappings),
        Some("application/vnd.vonk.custom")
    );
    assert_eq!(
        output_media_type("artifact.bin", &mappings),
        Some("application/octet-stream")
    );
    assert_eq!(output_media_type("report.PDF", &mappings), None);
    assert_eq!(
        output_media_type("artifact.VONK.bin", &mappings),
        Some("application/octet-stream")
    );
}

#[tokio::test]
async fn active_job_cancellation_runs_the_stop_path_promptly() {
    let (sender, mut cancellation) = tokio::sync::watch::channel(false);
    let (job_stopped, job_drained) = tokio::sync::oneshot::channel::<()>();
    let stopped = Arc::new(AtomicBool::new(false));
    let observed = Arc::clone(&stopped);
    tokio::spawn(async move {
        tokio::task::yield_now().await;
        sender.send_replace(true);
    });

    let result = tokio::time::timeout(
        Duration::from_secs(1),
        run_interruptible_job(
            async move {
                let _ = job_drained.await;
            },
            &mut cancellation,
            move || async move {
                observed.store(true, Ordering::Release);
                let _ = job_stopped.send(());
                Ok::<(), ()>(())
            },
        ),
    )
    .await
    .expect("cancellation did not interrupt the active job");

    assert!(matches!(
        result,
        InterruptibleJob::Cancelled { stopped: true }
    ));
    assert!(stopped.load(Ordering::Acquire));
}

#[test]
fn artifact_job_failure_keeps_current_result_and_typed_diagnostics() {
    let mut job_claim = claim();
    job_claim.operation = "recipe.job.run.v1".parse().unwrap();
    let envelope: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
    ))
    .unwrap();
    let mut receipt: vonk_agent_protocol::RecipeJobRunResult =
        serde_json::from_value(envelope["result"].clone()).unwrap();
    receipt.exit_code = 1;
    receipt.reason = Some("runtime failed".to_owned());
    let result = failed_outcome(
        &job_claim,
        ExecutionResult::Failed(
            Failure::new("runtime failed")
                .code(FailureCode::RecipeJobRunFailed)
                .receipt(receipt),
        ),
    );
    let typed = result.receipt.expect("the receipt is kept");
    typed.validate().unwrap();
    assert_eq!(typed.exit_code, 1);
    assert!(typed.diagnostics.is_some());
}

#[tokio::test]
async fn lost_cancellation_owner_stops_the_job_and_admits_a_fresh_operation() {
    let (sender, mut cancellation) = tokio::sync::watch::channel(false);
    drop(sender);
    let stopped = Arc::new(AtomicBool::new(false));
    let observed = Arc::clone(&stopped);
    let (finish, finished) = tokio::sync::oneshot::channel();
    let result = tokio::time::timeout(
        Duration::from_secs(1),
        run_interruptible_job(
            async {
                let _ = finished.await;
            },
            &mut cancellation,
            move || async move {
                observed.store(true, Ordering::SeqCst);
                let _ = finish.send(());
                Ok::<(), ()>(())
            },
        ),
    )
    .await
    .unwrap();
    assert!(matches!(
        result,
        InterruptibleJob::Cancelled { stopped: true }
    ));
    assert!(stopped.load(Ordering::SeqCst));
    let (_sender, mut fresh) = tokio::sync::watch::channel(false);
    assert!(matches!(
        run_interruptible_job(async { 7 }, &mut fresh, || async { Ok::<(), ()>(()) }).await,
        InterruptibleJob::Completed(7)
    ));
}
