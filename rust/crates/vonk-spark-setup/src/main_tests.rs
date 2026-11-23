use super::*;

#[test]
fn hidden_apply_is_the_only_privileged_cli_phase() {
    let cli = Cli::try_parse_from(["vonk-spark-setup", "__apply"]).unwrap();
    assert!(matches!(cli.command, Some(InternalCommand::Apply)));
    assert!(
        Cli::try_parse_from(["vonk-spark-setup", "--privileged"]).is_err(),
        "the former whole-installer root mode must not remain accepted"
    );
    assert!(
        Cli::try_parse_from([
            "vonk-spark-setup",
            "--release-manifest",
            "/tmp/release.json",
            "__apply",
        ])
        .is_err(),
        "public release arguments cannot be mixed into the root apply phase"
    );
}

#[test]
fn the_release_root_is_a_public_input_never_a_root_phase_one() {
    let cli = Cli::try_parse_from(["vonk-spark-setup", "--release-root", "/tmp/root.pem"]).unwrap();
    assert_eq!(cli.release_root, Some(PathBuf::from("/tmp/root.pem")));
    assert!(
        Cli::try_parse_from([
            "vonk-spark-setup",
            "--release-root",
            "/tmp/root.pem",
            "__apply"
        ])
        .is_err(),
        "the root phase reads its root only from its root-owned session"
    );
}

#[test]
fn enrollment_is_the_only_explicit_public_mode() {
    let cli = Cli::try_parse_from(["vonk-spark-setup", "--enroll"]).unwrap();
    assert!(cli.enroll);
    assert!(cli.command.is_none());
    assert!(Cli::try_parse_from(["vonk-spark-setup", "--enroll", "__apply"]).is_err());
    assert!(Cli::try_parse_from(["vonk-spark-setup", "--re-enroll"]).is_err());
}
