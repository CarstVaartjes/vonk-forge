# Install or upgrade a Spark

On the Spark, run exactly one command as the normal administrator account:

```sh
curl -fsSL https://install.vonkforge.ai/spark | sh
```

Do not prefix `curl` or the shell with `sudo`. The installer downloads the
published package as the current user, verifies its release identity and exact
digest, and only then asks `sudo` to install it.

For a new Spark, the wizard asks for the enrollment and controller endpoints,
controller CA URL and fingerprint, and one-use pairing token. Secret input is
hidden and never appears in process arguments or environment variables. The
token is the explicit enrollment authorization: the command exchanges it for
the Spark identity, starts the direct Rust-agent systemd service, and verifies
sustained readiness before returning.

For an installed Spark, run the same command. It preserves configuration and
identity, replaces the Debian package directly, restarts the service, and
requires sustained readiness. There is no separate upgrade command, A/B slot,
supervisor, rollback state, migration command, or follow-up setup step.

If the command fails, read its final error and rerun the same command after
correcting that condition. It fails before privilege escalation when release
verification does not pass.
