#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn runtime_accepts_exact_single_and_multiple_artifact_mounts() {
    let (_temp, roots) = runtime_fixture();
    let model = artifact_path(&roots, 'a');
    let tokenizer = artifact_path(&roots, 'b');
    fs::create_dir_all(&model).unwrap();
    fs::create_dir_all(&tokenizer).unwrap();

    let single = runtime_arguments(&roots, &[(model.clone(), "/models", true)]);
    let validated = validate_docker_run(&single, &roots, None).unwrap();
    assert_eq!(validated.models, vec![model.clone()]);

    let multiple = runtime_arguments(
        &roots,
        &[
            (model.clone(), "/models/model", true),
            (tokenizer.clone(), "/models/tokenizer-v2", true),
        ],
    );
    let validated = validate_docker_run(&multiple, &roots, None).unwrap();
    assert_eq!(validated.models, vec![model, tokenizer]);
}

#[test]
fn runtime_accepts_selection_scoped_nested_model_files_beyond_legacy_limit() {
    let (_temp, roots) = runtime_fixture();
    let model_set = runtime_models(&roots).join("a".repeat(64));
    let primary = model_set.join("primary");
    let draft = model_set.join("draft");
    fs::create_dir_all(&primary).unwrap();
    fs::create_dir_all(&draft).unwrap();
    let mut mounts = Vec::new();
    for index in 0..132 {
        let name = format!("artifact-{index}.bin");
        let source = if index == 0 {
            primary.join(&name)
        } else {
            draft.join(&name)
        };
        fs::write(&source, b"identical receipt bytes").unwrap();
        let target = if index == 0 {
            format!("/models/{name}")
        } else {
            format!("/models/draft/{name}")
        };
        mounts.push((source, target));
    }
    let mounts = mounts
        .iter()
        .map(|(source, target)| (source.clone(), target.as_str(), true))
        .collect::<Vec<_>>();
    let validated = validate_docker_run(&runtime_arguments(&roots, &mounts), &roots, None)
        .expect("selection-scoped model files should remain independently mountable");
    assert_eq!(validated.models.len(), 132);
}

#[test]
fn runtime_accepts_mixed_case_long_model_filename() {
    let (_temp, roots) = runtime_fixture_with_separate_agent_data();
    let filename = "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf";
    let source = runtime_models(&roots).join("primary").join(filename);
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    fs::write(&source, b"model fixture").unwrap();
    let target = format!("/models/primary/{filename}");
    assert!(
        validate_docker_run(
            &runtime_arguments(&roots, &[(source, &target, true)]),
            &roots,
            None,
        )
        .is_ok()
    );
}

#[test]
fn runtime_accepts_canonical_unicode_space_and_underscore_model_filename() {
    let (_temp, roots) = runtime_fixture_with_separate_agent_data();
    let filename = "模型 weights_file.safetensors";
    let source = runtime_models(&roots).join("primary").join(filename);
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    fs::write(&source, b"model fixture").unwrap();
    let target = format!("/models/primary/{filename}");
    assert!(
        validate_docker_run(
            &runtime_arguments(&roots, &[(source, &target, true)]),
            &roots,
            None,
        )
        .is_ok()
    );
}

#[test]
fn runtime_rejects_noncanonical_or_unsafe_artifact_mounts() {
    let (_temp, roots) = runtime_fixture();
    let model = artifact_path(&roots, 'a');
    let tokenizer = artifact_path(&roots, 'b');
    fs::create_dir_all(&model).unwrap();
    fs::create_dir_all(&tokenizer).unwrap();

    let invalid_single_mounts = [
        (runtime_models(&roots), "/models", true),
        (runtime_models(&roots).join("sha256"), "/models", true),
        (runtime_models(&roots).join("a".repeat(64)), "/models", true),
        (
            roots
                .agent_data
                .join("installations")
                .join("installation-1")
                .join("models")
                .join("sha256")
                .join("..")
                .join("sha256")
                .join("a".repeat(64)),
            "/models",
            true,
        ),
        (
            runtime_models(&roots)
                .join("Primary")
                .join("artifact-a.bin"),
            "/models",
            true,
        ),
        (model.clone(), "/models", false),
        (model.clone(), "/model", true),
        (model.clone(), "/models/..", true),
        (model.clone(), "/models/model/", true),
    ];
    for mount in invalid_single_mounts {
        assert!(validate_docker_run(&runtime_arguments(&roots, &[mount]), &roots, None).is_err());
    }

    for mounts in [
        vec![
            (model.clone(), "/models/model", true),
            (model.clone(), "/models/tokenizer", true),
        ],
        vec![
            (model.clone(), "/models/model", true),
            (tokenizer.clone(), "/models/model", true),
        ],
        vec![
            (model.clone(), "/models", true),
            (tokenizer.clone(), "/models/tokenizer", true),
        ],
    ] {
        assert!(validate_docker_run(&runtime_arguments(&roots, &mounts), &roots, None).is_err());
    }

    let too_many = (0..4097)
        .map(|index| {
            let source = runtime_models(&roots)
                .join("primary")
                .join(format!("artifact-{index}.bin"));
            fs::create_dir(&source).unwrap();
            (source, format!("/models/artifact-{index}"))
        })
        .collect::<Vec<_>>();
    let too_many = too_many
        .iter()
        .map(|(source, target)| (source.clone(), target.as_str(), true))
        .collect::<Vec<_>>();
    assert!(validate_docker_run(&runtime_arguments(&roots, &too_many), &roots, None).is_err());

    let symlinked = artifact_path(&roots, 'd');
    symlink(&model, &symlinked).unwrap();
    assert!(
        validate_docker_run(
            &runtime_arguments(&roots, &[(symlinked, "/models", true)]),
            &roots,
            None,
        )
        .is_err()
    );
}

#[test]
fn runtime_accepts_installation_model_root_and_rejects_legacy_model_root() {
    let (_temp, roots) = runtime_fixture_with_separate_agent_data();
    let current_model = artifact_path(&roots, 'a');
    fs::create_dir_all(&current_model).unwrap();
    assert!(
        validate_docker_run(
            &runtime_arguments(&roots, &[(current_model, "/models", true)]),
            &roots,
            None,
        )
        .is_ok()
    );

    let legacy_model = roots
        .data
        .join("models")
        .join("sha256")
        .join("b".repeat(64));
    fs::create_dir_all(&legacy_model).unwrap();
    assert!(
        validate_docker_run(
            &runtime_arguments(&roots, &[(legacy_model, "/models", true)]),
            &roots,
            None,
        )
        .is_err()
    );
}

#[test]
fn runtime_rejects_legacy_sha256_model_layout() {
    let (_temp, roots) = runtime_fixture();
    let legacy_model = runtime_models(&roots).join("sha256").join("a".repeat(64));
    fs::create_dir_all(&legacy_model).unwrap();
    assert!(
        validate_docker_run(
            &runtime_arguments(&roots, &[(legacy_model, "/models", true)]),
            &roots,
            None,
        )
        .is_err()
    );
}

#[test]
fn runtime_rejects_symlinked_model_ancestors_and_canonical_escapes() {
    {
        let (temp, roots) = runtime_fixture();
        let models = runtime_models(&roots);
        fs::remove_dir_all(&models).unwrap();
        let outside_models = temp.path().join("outside-models");
        let outside_model = outside_models.join("primary").join("artifact-a.bin");
        fs::create_dir_all(&outside_model).unwrap();
        symlink(&outside_models, &models).unwrap();

        let mount = artifact_path(&roots, 'a');
        assert!(
            validate_docker_run(
                &runtime_arguments(&roots, &[(mount, "/models", true)]),
                &roots,
                None,
            )
            .is_err()
        );
    }

    {
        let (temp, roots) = runtime_fixture();
        let model_root = runtime_models(&roots).join("primary");
        fs::remove_dir(&model_root).unwrap();
        let outside_model_root = temp.path().join("outside-primary");
        fs::create_dir_all(outside_model_root.join("artifact-a.bin")).unwrap();
        symlink(&outside_model_root, &model_root).unwrap();

        let mount = artifact_path(&roots, 'a');
        assert!(
            validate_docker_run(
                &runtime_arguments(&roots, &[(mount, "/models", true)]),
                &roots,
                None,
            )
            .is_err()
        );
    }

    {
        let (_temp, roots) = runtime_fixture_with_separate_agent_data();
        let installation = roots
            .agent_data
            .join("installations")
            .join("installation-1");
        let sibling = roots
            .agent_data
            .join("installations")
            .join("installation-2");
        fs::create_dir_all(sibling.join("models").join("sha256")).unwrap();
        fs::remove_dir_all(&installation).unwrap();
        symlink(&sibling, &installation).unwrap();
        let model = installation
            .join("models")
            .join("primary")
            .join("artifact-a.bin");
        fs::create_dir_all(&model).unwrap();
        assert!(
            validate_docker_run(
                &runtime_arguments(&roots, &[(model, "/models", true)]),
                &roots,
                None,
            )
            .is_err()
        );
    }

    {
        let temp = tempfile::tempdir().unwrap();
        let outside = temp.path().join("outside-agent-data");
        let agent_data = temp.path().join("agent-data-link");
        let roots = ManagedRoots::under(&agent_data);
        let model = outside
            .join("installations")
            .join("installation-1")
            .join("models")
            .join("sha256")
            .join("a".repeat(64));
        fs::create_dir_all(&model).unwrap();
        fs::create_dir_all(outside.join("runs").join(RUN_ID).join("outputs")).unwrap();
        let metadata = outside.join("run-metadata").join(RUN_ID);
        fs::create_dir_all(&metadata).unwrap();
        fs::write(metadata.join("runtime.json"), b"{}").unwrap();
        symlink(&outside, &agent_data).unwrap();

        let mount = artifact_path(&roots, 'a');
        assert!(
            validate_docker_run(
                &runtime_arguments(&roots, &[(mount, "/models", true)]),
                &roots,
                None,
            )
            .is_err()
        );
    }
}

#[test]
fn runtime_accepts_canonical_nested_model_path_at_512_characters() {
    let (_temp, roots) = runtime_fixture_with_separate_agent_data();
    let segment = format!("模_{}", "a".repeat(61));
    let final_segment = format!("模_{}", "a".repeat(62));
    let mut segments = vec![segment; 7];
    segments.push(final_segment);
    let relative = segments.join("/");
    assert_eq!(relative.chars().count(), MAX_COMPILED_MODEL_PATH_CHARS);
    let source = runtime_models(&roots).join("primary").join(&relative);
    fs::create_dir_all(source.parent().unwrap()).unwrap();
    fs::write(&source, b"model fixture").unwrap();
    let target = "/models/primary";
    assert!(
        validate_docker_run(
            &runtime_arguments(&roots, &[(source, target, true)]),
            &roots,
            None,
        )
        .is_ok()
    );
}

#[test]
fn runtime_rejects_unsafe_model_ownership_and_modes() {
    let (_temp, roots) = runtime_fixture();
    let model = artifact_path(&roots, 'a');
    fs::create_dir_all(&model).unwrap();
    let arguments = runtime_arguments(&roots, &[(model.clone(), "/models", true)]);

    let owner = fs::symlink_metadata(&roots.agent_data).unwrap().uid();
    assert!(validate_docker_run(&arguments, &roots, Some(owner ^ 1)).is_err());
    validate_docker_run(&arguments, &roots, Some(owner)).unwrap();

    for path in [
        roots.agent_data.clone(),
        roots.agent_data.join("installations"),
        runtime_models(&roots),
        runtime_models(&roots).join("primary"),
        model,
    ] {
        fs::set_permissions(&path, fs::Permissions::from_mode(0o770)).unwrap();
        assert!(validate_docker_run(&arguments, &roots, Some(owner)).is_err());
        fs::set_permissions(&path, fs::Permissions::from_mode(0o750)).unwrap();
    }
}
