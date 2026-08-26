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
fingerprint, one-use pairing token, Spark management IPv4 address, this Spark's
fabric IPv4 address, and its peer's fabric IPv4 address. The generated private
NAS command supplies `VONK_CONTROLLER_ADDRESS`, so that address also becomes the
NAS management address without another prompt. If it is absent, the wizard asks
for the NAS management address too. The endpoint ports (`8000,8101` and `8888`),
rendezvous port (`29500`), and fabric bandwidth (`200000` Mbit/s) use validated
defaults.

Secret input is hidden and never appears in process arguments or environment
variables. The installer retrieves the bounded bootstrap document through the
controller-supplied NAS address, requires its CA to match the out-of-band
fingerprint, repeats discovery over verified TLS, and writes the root-owned agent,
firewall, and host-helper trust configuration. It also installs an idempotent
managed hostname mapping for the controller services. The HTTPS URLs continue to
use their certificate names; no Spark-side Tailscale installation, manual DNS
edit, or separate configuration step is required. The token is the explicit
enrollment authorization: the command exchanges it for the Spark identity,
starts the firewall, package-helper, and direct Rust-agent systemd units, and
verifies sustained readiness before returning.

The plain public command remains valid when the controller hostnames already
resolve from the Spark:

```sh
curl -fsSL https://install.vonkforge.ai/spark | sh
```

For an installed Spark, rerun the command shown by the controller. It preserves configuration and
identity, replaces the Debian package directly, restarts the service, and
requires sustained readiness. There is no separate upgrade command, A/B slot,
supervisor, rollback state, migration command, or follow-up setup step.

An ordinary rerun is only an upgrade. Certificate recovery is deliberately
explicit: create a **Re-enroll Spark** grant in Fleet and run its command, which
passes `--re-enroll` through the signed channel bootstrap. This replaces stale
credentials and resets a failed systemd start limit automatically. The generated
URL is `/dev/spark` for a development NAS and `/spark` for a stable NAS.

If the command fails, read its final error and rerun the same command after
correcting that condition. It fails before privilege escalation when release
verification does not pass.
