use super::CompiledOciError;

/// Kernel limits that apply to one `execve` argument/environment block.
///
/// The caller supplies the limits reported by the target Linux runtime (for
/// example, `_SC_ARG_MAX` and its `MAX_ARG_STRLEN` equivalent). This keeps
/// this shared protocol module pure while letting both the agent and helper
/// measure the exact same projection against the actual host boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExecInvocationLimits {
    /// Maximum total bytes for strings and the argv/envp pointer tables.
    pub total_bytes: u64,
    /// Maximum bytes in any one NUL-terminated argument or environment string.
    pub string_bytes: u64,
}

/// Accounted bytes for one exact `execve` invocation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExecInvocationUsage {
    pub argument_string_bytes: u64,
    pub environment_string_bytes: u64,
    pub pointer_bytes: u64,
    pub total_bytes: u64,
    pub largest_string_bytes: u64,
}

/// Measure the exact UTF-8 argv/environment that will be passed to `execve`.
///
/// `argv0` is the executable path and `arguments` contains only argv[1..]; the
/// caller also supplies the final helper environment. Each string includes
/// its terminating NUL, and the accounting includes both null-terminated
/// pointer tables. Interior NUL bytes are rejected because the OS would
/// otherwise observe a truncated value.
pub fn measure_exec_invocation(
    argv0: &str,
    arguments: &[String],
    environment: &[(&str, &str)],
    limits: ExecInvocationLimits,
) -> Result<ExecInvocationUsage, CompiledOciError> {
    if argv0.is_empty() || argv0.contains('\0') {
        return Err(CompiledOciError::Invalid("exec argv[0] is invalid"));
    }
    let mut argument_string_bytes = 0_u64;
    let mut environment_string_bytes = 0_u64;
    let mut largest_string_bytes = 0_u64;

    let mut account_string = |value: &str| -> Result<u64, CompiledOciError> {
        if value.contains('\0') {
            return Err(CompiledOciError::Invalid("exec string contains NUL"));
        }
        let observed = u64::try_from(value.len())
            .ok()
            .and_then(|length| length.checked_add(1))
            .ok_or(CompiledOciError::Invalid("exec string byte count overflow"))?;
        if observed > limits.string_bytes {
            return Err(CompiledOciError::InvocationStringBytes {
                limit: limits.string_bytes,
                observed,
            });
        }
        largest_string_bytes = largest_string_bytes.max(observed);
        Ok(observed)
    };

    argument_string_bytes = argument_string_bytes
        .checked_add(account_string(argv0)?)
        .ok_or(CompiledOciError::Invalid("exec byte count overflow"))?;
    for argument in arguments {
        argument_string_bytes = argument_string_bytes
            .checked_add(account_string(argument)?)
            .ok_or(CompiledOciError::Invalid("exec byte count overflow"))?;
    }
    for (name, value) in environment {
        if name.is_empty() || name.contains('=') || name.contains('\0') {
            return Err(CompiledOciError::Invalid(
                "exec environment name is invalid",
            ));
        }
        if value.contains('\0') {
            return Err(CompiledOciError::Invalid(
                "exec environment value contains NUL",
            ));
        }
        let observed = name
            .len()
            .checked_add(1)
            .and_then(|length| length.checked_add(value.len()))
            .and_then(|length| length.checked_add(1))
            .ok_or(CompiledOciError::Invalid("exec byte count overflow"))?;
        let observed = u64::try_from(observed)
            .map_err(|_| CompiledOciError::Invalid("exec byte count overflow"))?;
        if observed > limits.string_bytes {
            return Err(CompiledOciError::InvocationStringBytes {
                limit: limits.string_bytes,
                observed,
            });
        }
        largest_string_bytes = largest_string_bytes.max(observed);
        environment_string_bytes = environment_string_bytes
            .checked_add(observed)
            .ok_or(CompiledOciError::Invalid("exec byte count overflow"))?;
    }

    let pointer_count = arguments
        .len()
        .checked_add(environment.len())
        .and_then(|count| count.checked_add(3))
        .ok_or(CompiledOciError::Invalid("exec pointer count overflow"))?;
    let pointer_bytes = u64::try_from(pointer_count)
        .ok()
        .and_then(|count| count.checked_mul(std::mem::size_of::<usize>() as u64))
        .ok_or(CompiledOciError::Invalid(
            "exec pointer byte count overflow",
        ))?;
    let total_bytes = argument_string_bytes
        .checked_add(environment_string_bytes)
        .and_then(|bytes| bytes.checked_add(pointer_bytes))
        .ok_or(CompiledOciError::Invalid("exec byte count overflow"))?;
    if total_bytes > limits.total_bytes {
        return Err(CompiledOciError::InvocationBytes {
            limit: limits.total_bytes,
            observed: total_bytes,
        });
    }

    Ok(ExecInvocationUsage {
        argument_string_bytes,
        environment_string_bytes,
        pointer_bytes,
        total_bytes,
        largest_string_bytes,
    })
}
