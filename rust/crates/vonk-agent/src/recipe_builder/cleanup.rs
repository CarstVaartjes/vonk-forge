//! Cleanup.

use super::*;

/// A Controller-authorized cleanup names a build, never a host path or
/// arbitrary service. Systemd stops the entire build cgroup before capacity is
/// released; a failed query or stop is not evidence that the service is absent.
pub fn cleanup_build<R: ProcessRunner>(
    runner: &R,
    request: &RecipeBuildCleanupRequest,
) -> Result<RecipeBuildCleanupEvidence, RecipeBuildError> {
    let unit = format!("vonk-recipe-build-{}.service", request.build_id);
    let query = vec![
        "--user".to_owned(),
        "show".to_owned(),
        unit.clone(),
        "--property=LoadState".to_owned(),
        "--property=ActiveState".to_owned(),
        "--property=MainPID".to_owned(),
    ];
    let observe = || -> Result<bool, RecipeBuildError> {
        let listed = runner.run(
            Program::Systemctl,
            &[
                "--user".to_owned(),
                "list-units".to_owned(),
                "--all".to_owned(),
                "--plain".to_owned(),
                "--no-legend".to_owned(),
                "--no-pager".to_owned(),
                "--full".to_owned(),
                unit.clone(),
            ],
            Duration::from_secs(10),
        )?;
        if !listed.success {
            return Err(RecipeBuildError::Evidence);
        }
        let listed_text =
            std::str::from_utf8(&listed.stdout).map_err(|_| RecipeBuildError::Evidence)?;
        if listed_text.trim().is_empty() {
            return Ok(true);
        }
        if listed_text.lines().count() != 1
            || listed_text.split_whitespace().next() != Some(unit.as_str())
        {
            return Err(RecipeBuildError::Evidence);
        }
        let output = runner.run(Program::Systemctl, &query, Duration::from_secs(10))?;
        if !output.success {
            return Err(RecipeBuildError::Evidence);
        }
        let text = std::str::from_utf8(&output.stdout).map_err(|_| RecipeBuildError::Evidence)?;
        let fields: std::collections::BTreeMap<_, _> = text
            .lines()
            .filter_map(|line| line.split_once('='))
            .collect();
        if fields.len() != 3 || text.lines().count() != 3 {
            return Err(RecipeBuildError::Evidence);
        }
        Ok(
            matches!(fields.get("ActiveState"), Some(&"inactive" | &"failed"))
                && fields.get("MainPID") == Some(&"0"),
        )
    };
    if !observe()? {
        let output = runner.run(
            Program::Systemctl,
            &["--user".to_owned(), "stop".to_owned(), unit.clone()],
            Duration::from_secs(15),
        )?;
        if !output.success || !observe()? {
            return Err(RecipeBuildError::Evidence);
        }
    }
    Ok(RecipeBuildCleanupEvidence {})
}
