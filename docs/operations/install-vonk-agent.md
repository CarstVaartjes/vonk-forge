# Install or upgrade a Spark

Create a one-use grant in the controller Fleet page. On the Spark, run the exact
command shown by the controller as the normal administrator account. A private
NAS command looks like:

```sh
curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS=192.168.1.231 sh
```

Do not prefix `curl` or the shell with `sudo`. The installer downloads the
published package as the current user, verifies its release identity and exact
digest, and only then asks `sudo` to install it.

For a new Spark, the wizard asks for the enrollment endpoint, controller CA
fingerprint, and one-use pairing token. Secret input is hidden and never
appears in process arguments or environment variables. The installer retrieves
the bounded bootstrap document through the controller-supplied NAS address,
requires its CA to match the out-of-band fingerprint, repeats discovery over
verified TLS, and installs an idempotent managed hostname mapping for the
controller services. The HTTPS URLs continue to use their certificate names;
no Spark-side Tailscale installation or manual DNS edit is required. The token
is the explicit enrollment authorization: the command
exchanges it for the Spark identity, starts the direct Rust-agent systemd
service, and verifies sustained readiness before returning.

The plain public command remains valid when the controller hostnames already
resolve from the Spark:

```sh
curl -fsSL https://install.vonkforge.ai/spark | sh
```

For an installed Spark, rerun the command shown by the controller. It preserves configuration and
identity, replaces the Debian package directly, restarts the service, and
requires sustained readiness. There is no separate upgrade command, A/B slot,
supervisor, rollback state, migration command, or follow-up setup step.

If the command fails, read its final error and rerun the same command after
correcting that condition. It fails before privilege escalation when release
verification does not pass.
