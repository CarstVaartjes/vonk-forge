#![cfg(test)]

use super::*;

#[test]
fn projected_start_measures_the_linux_environment_e2big_boundary() {
    // Wrong implementation: measure only runtime.argv, omitting the
    // projected env/mount/Podman tokens, argv[0], fixed helper env, NULs,
    // and pointer tables. The canonical plan validates at both points;
    // the exact projected invocation is what crosses the Linux boundary.
    const LINUX_ARG_MAX: u64 = 2_097_152;
    const LINUX_MAX_ARG_STRLEN: u64 = 131_072;
    let limits = ExecInvocationLimits {
        total_bytes: LINUX_ARG_MAX,
        string_bytes: LINUX_MAX_ARG_STRLEN,
    };
    let measured = |extra_env_count: usize| {
        let mut value = fixture();
        for index in 0..extra_env_count {
            let prefix = format!("VONK_BULK_{index:02}");
            let name = format!("{prefix}{}", "X".repeat(128 - prefix.len()));
            value["runtime"]["env"]
                .as_array_mut()
                .unwrap()
                .push(json!({"name": name, "value": "x".repeat(65_536)}));
        }
        let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        plan.validate().unwrap();
        let arguments =
            start_arguments_for_paths(&plan, &paths(), "10000000-0000-4000-8000-000000000001")
                .unwrap();
        measure_exec_invocation("/usr/bin/docker", &arguments, &helper_environment(), limits)
    };

    let just_under = measured(31).expect("31 additions fit the measured Linux limit");
    assert!(just_under.total_bytes < LINUX_ARG_MAX);
    assert!(just_under.largest_string_bytes <= LINUX_MAX_ARG_STRLEN);
    assert!(matches!(
        measured(32),
        Err(CompiledOciError::InvocationBytes {
            limit: LINUX_ARG_MAX,
            observed
        }) if observed > LINUX_ARG_MAX
    ));
}

#[test]
fn exec_accounting_accepts_empty_and_multiline_arguments_at_exact_limit() {
    // Wrong implementation: normalize or reject argument strings that the
    // canonical contract preserves, or omit NUL/pointer bytes and accept
    // an invocation one entry beyond the exact measured limit.
    let args = vec![String::new(), "line one\nline two".to_owned()];
    let environment = helper_environment();
    let unbounded = ExecInvocationLimits {
        total_bytes: u64::MAX,
        string_bytes: u64::MAX,
    };
    let usage = measure_exec_invocation("/usr/bin/docker", &args, &environment, unbounded).unwrap();
    let exact = ExecInvocationLimits {
        total_bytes: usage.total_bytes,
        string_bytes: usage.largest_string_bytes,
    };
    assert_eq!(
        measure_exec_invocation("/usr/bin/docker", &args, &environment, exact).unwrap(),
        usage
    );

    let mut one_more = args;
    one_more.push("x".to_owned());
    let expected_over = usage.total_bytes + 2 + std::mem::size_of::<usize>() as u64;
    assert!(matches!(
        measure_exec_invocation("/usr/bin/docker", &one_more, &environment, exact),
        Err(CompiledOciError::InvocationBytes { limit, observed })
            if limit == usage.total_bytes && observed == expected_over
    ));
}

#[test]
fn exec_accounting_enforces_single_string_limit_and_rejects_nul() {
    let limits = ExecInvocationLimits {
        total_bytes: 1024,
        string_bytes: 4,
    };
    let exact = measure_exec_invocation("p", &["abc".to_owned()], &[], limits).unwrap();
    assert_eq!(exact.largest_string_bytes, 4);
    assert!(matches!(
        measure_exec_invocation("p", &["abcd".to_owned()], &[], limits),
        Err(CompiledOciError::InvocationStringBytes {
            limit: 4,
            observed: 5
        })
    ));
    assert!(matches!(
        measure_exec_invocation("p", &["a\0b".to_owned()], &[], limits),
        Err(CompiledOciError::Invalid("exec string contains NUL"))
    ));
    assert!(matches!(
        measure_exec_invocation("p", &[], &[("LANG", "C\0.UTF-8")], limits),
        Err(CompiledOciError::Invalid(
            "exec environment value contains NUL"
        ))
    ));
}
