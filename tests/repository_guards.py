"""Reviewed CI coverage inventory, independent of Git history.

GUARDS are fast cross-area scans (or scan nodes in mixed integration modules).
SCOPED names every other test file with its owning boundary. New files require
an explicit coverage decision, even when their scan is hidden behind a helper.
Do not add a whole-tree scan to SCOPED: select its file or node in GUARDS.
"""

GUARDS: tuple[str, ...] = (
    "tests/test_ci_apt_install.py",
    "tests/cluster_profiles/test_cli_render.py::test_every_controller_command_has_a_registered_presentation",
    "tests/test_controller_startup_guard.py",
    "tests/test_gpu_telemetry_unit.py",
    "tests/test_data_contract_guards.py",
    "tests/test_file_size_ratchet.py",
    "tests/test_git_hermeticity.py",
    "tests/test_orm_mapping_guard.py",
    "tests/test_run_switch_patch_targets.py",
    "tests/test_cli_package_boundaries.py",
    "tests/test_no_prototype_model_authority.py",
    "tests/test_shell_destructive_guard.py",
    "tests/test_workflow_assertions.py",
    "tests/test_workflow_event_context.py",
    "tests/test_workflow_script_environments.py",
    "tests/test_release_workflow.py",
    "tests/test_repository_guard_inventory.py",
    "tests/scripts/test_select_ci_areas.py",
    "tests/scripts/test_verify_ci_gate.py",
    "tests/scripts/test_build_control_wheel.py::test_every_control_workflow_builds_the_wheel_before_uv",
    "tests/scripts/test_hook_environment.py::test_scripts_cannot_delegate_automatic_preparation_to_users",
    "tests/test_agent_release_workflow.py::test_release_actions_are_commit_pinned_and_secrets_are_environment_scoped",
)

SCOPED: tuple[tuple[str, str], ...] = (
    (
        "tests/scripts/test_vm_cargo_hook.py",
        "VM cargo hook command and fault classification; no host services.",
    ),
    (
        "tests/acceptance/test_candidate_recipe_e2e.py",
        "Physical acceptance harness; belongs to the acceptance lane.",
    ),
    (
        "tests/acceptance/test_fresh_nas_install.py",
        "Physical acceptance harness; belongs to the acceptance lane.",
    ),
    (
        "tests/acceptance/test_spark_lifecycle.py",
        "Physical acceptance harness; belongs to the acceptance lane.",
    ),
    (
        "tests/cluster_profiles/test_cli_artifact_jobs.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_cache_submission.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_enrollment.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_gateway_keys.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_process.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_profile_load.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_profile_observation.py",
        "Bounded CLI successor observation and recovery; no whole-tree scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_profile_review_binding.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_profile_selection.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_profiles.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_recipe_options.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_render.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_selection.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_states.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_cli_update.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_control_client_requests.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_control_transport_deadline.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_controller_cli.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_error_reporting.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_fleet_qualification_campaign_cli.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_glb_validation.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_observation_attempt_deadline.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_qualification_fixtures.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_schema_packaging.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/cluster_profiles/test_ssh_transport.py",
        "CLI consumer behavior and packaged contracts; no whole-tree product scan.",
    ),
    (
        "tests/control/test_openapi_clients.py",
        "Generated Controller clients; generation lane owns these inputs.",
    ),
    (
        "tests/install/test_curl_bootstraps.py",
        "Installer bootstrap and signed handoff behavior.",
    ),
    (
        "tests/install/test_signed_release_handoff.py",
        "Installer bootstrap and signed handoff behavior.",
    ),
    (
        "tests/nodes/test_agent_boot_restart_systemd.py",
        "Native agent lifecycle; belongs to the systemd/Linux lane.",
    ),
    (
        "tests/nodes/test_agent_package_rollback_systemd.py",
        "Native agent lifecycle; belongs to the systemd/Linux lane.",
    ),
    (
        "tests/nodes/test_helper_runtime_filesystem.py",
        "Native agent lifecycle; belongs to the systemd/Linux lane.",
    ),
    (
        "tests/scripts/test_agent_apt_compaction_integration.py",
        "agent apt compaction integration: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_agent_apt_metadata.py",
        "agent apt metadata: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_agent_apt_state.py",
        "agent apt state: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_agent_deb.py",
        "agent deb: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_agent_package_metadata.py",
        "agent package metadata: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_agent_repair_lifecycle.py",
        "agent repair lifecycle: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_build_control_wheel.py",
        "build control wheel: fixed entrypoint/fixture behavior. The cross-area scan is selected separately in GUARDS.",
    ),
    (
        "tests/scripts/test_build_nas_compose_bundle.py",
        "build nas compose bundle: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_check_python_types.py",
        "check python types: fixed entrypoint/fixture behavior.",
    ),
    ("tests/scripts/test_ci_ruff.py", "ci ruff: fixed entrypoint/fixture behavior."),
    (
        "tests/scripts/test_ci_script_recovery.py",
        "ci script recovery: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_collect_pytest_file_durations.py",
        "collect pytest file durations: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_container_release_metadata.py",
        "container release metadata: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_dev_agent_wire_linux.py",
        "dev agent wire linux: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_hook_environment.py",
        "hook environment: fixed entrypoint/fixture behavior. The cross-area scan is selected separately in GUARDS.",
    ),
    (
        "tests/scripts/test_install_release_publication.py",
        "install release publication: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_lifecycle_canary.py",
        "lifecycle canary: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_lifecycle_counts.py",
        "lifecycle counts: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_managed_ca_release_contract.py",
        "managed ca release contract: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_promote_accepted_channel.py",
        "promote accepted channel: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_promote_image_aliases.py",
        "promote image aliases: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_publication_dev_ancestry.py",
        "publication dev ancestry: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_publication_stream_deadline.py",
        "publication stream deadline: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_qualify_recipe.py",
        "qualify recipe: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_release_reconcilers.py",
        "release reconcilers: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_render_accepted_compose_overlay.py",
        "render accepted compose overlay: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_render_dev_compose.py",
        "render dev compose: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_render_install_bootstrap.py",
        "render install bootstrap: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_render_production_compose.py",
        "render production compose: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_repair_capsule_publication.py",
        "repair capsule publication: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_resolve_publication_producer.py",
        "resolve publication producer: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_restore_pytest_file_durations.py",
        "restore pytest file durations: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_retry_dependency_fetch.py",
        "retry dependency fetch: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_select_pytest_shard_files.py",
        "select pytest shard files: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_validate_fabric.py",
        "validate fabric: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_validate_recipe_library.py",
        "validate recipe library: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_verify_agent_binaries.py",
        "verify agent binaries: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_verify_agent_systemd.py",
        "verify agent systemd: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_verify_controller_skopeo.py",
        "verify controller skopeo: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_verify_public_image_inputs.py",
        "verify public image inputs: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_verify_release_tag_authority.py",
        "verify release tag authority: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/scripts/test_verify_supply_chain.py",
        "verify supply chain: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_acceptance_controller_contract.py",
        "acceptance controller contract: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_acceptance_observation_transfer.py",
        "acceptance observation transfer: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_acceptance_runtime.py",
        "acceptance runtime: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_acceptance_systemd_sandbox.py",
        "acceptance systemd sandbox: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_agent_release_workflow.py",
        "agent release workflow: fixed entrypoint/fixture behavior. The cross-area scan is selected separately in GUARDS.",
    ),
    (
        "tests/test_cli_dependencies_platform.py",
        "cli dependencies platform: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_container_release_workflow.py",
        "container release workflow: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_dependabot_updates.py",
        "dependabot updates: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_fresh_install_legacy_boundary.py",
        "fresh install legacy boundary: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_fresh_nas_acceptance.py",
        "fresh nas acceptance: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_generated_contracts.py",
        "generated contracts: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_lifecycle_counts_workflow.py",
        "lifecycle counts workflow: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_lifecycle_vocabulary_generation.py",
        "lifecycle vocabulary generation: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_pytest_budget.py",
        "pytest budget: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_reason_code_generation.py",
        "reason code generation: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_recipe_library_ci_receipt.py",
        "recipe library ci receipt: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_required_acceptance_execution.py",
        "required acceptance execution: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_serving_execution.py",
        "serving execution: fixed entrypoint/fixture behavior.",
    ),
    ("tests/test_shell_suites.py", "shell suites: fixed entrypoint/fixture behavior."),
    (
        "tests/test_spark_lifecycle_contract.py",
        "spark lifecycle contract: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_spark_lifecycle_diagnostics.py",
        "spark lifecycle diagnostics: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_spark_lifecycle_runner.py",
        "spark lifecycle runner: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_spark_upgrade_carry.py",
        "spark upgrade carry: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_spark_upgrade_carry_release_graph.py",
        "spark upgrade carry release graph: fixed entrypoint/fixture behavior.",
    ),
    (
        "tests/test_tailscale_acceptance_tailnet.py",
        "tailscale acceptance tailnet: fixed entrypoint/fixture behavior.",
    ),
)
