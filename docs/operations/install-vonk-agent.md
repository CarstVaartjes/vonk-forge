# Install or upgrade a Spark

Create a one-use grant in the controller Fleet page. On the Spark, run the exact
command shown by the controller as the normal administrator account. A private
NAS command looks like:

```sh
curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS=192.168.1.231 VONK_ENROLLMENT_URL=https://enroll.example.test VONK_CONTROLLER_CA_SHA256=<fingerprint> sh -s -- --enroll
```

Do not prefix `curl` or the shell with `sudo`. The installer downloads the
published package as the current user, verifies its release identity and exact
digest, and then authenticates `sudo` in the foreground terminal. The framed
privileged handoff runs noninteractively; an expired sudo ticket fails clearly
without treating the enrollment frame as a password prompt.

For a new Spark, the generated command supplies the NAS address, enrollment
endpoint and controller CA fingerprint, so the installer asks only for the
one-use pairing token. It detects the Spark's own addresses: the management
address is the one the kernel uses to reach the NAS, the fabric address is the
only IPv4 address on an active RoCE interface, and the peer is the other end of
a point-to-point fabric subnet or the fabric link's only known neighbour. It
asks for any of them only when detection finds none or more than one, and for
the enrollment values only when they are missing from the command. The endpoint
ports (`8000,8101` and `8888`), rendezvous port (`29500`), and fabric bandwidth
(`200000` Mbit/s) are fixed.

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

For routine upgrades of enrolled online Sparks, use Fleet's **Upgrade agents**
action or `vonkctl fleet upgrade`. It previews the exact signed package and
eligible nodes, rolls out one Spark at a time by default, and requires the new
agent and privileged helper activation evidence before continuing. This path
preserves configuration and identity and does not require SSH.

Connected idle agents refresh their hardware inventory after two minutes, with
at most one additional minute for an in-flight idle claim. This keeps builder
and workload admission current without restarting the agent. A failed report
does not advance the refresh deadline; normal bounded retries still apply.
The systemd unit uses `Type=notify` and a watchdog. The agent reports readiness
after local state recovery, then refreshes its watchdog only after a completed
control-loop step or a handled retry. Startup inventory failures such as a late
Podman/NVIDIA/CDI runtime stay in-process with bounded exponential backoff.
Low free space on the state database filesystem is reported in systemd status
and authenticated inventory. The agent holds a 64 MiB reserve file when at
least 128 MiB is free, releases it below 64 MiB to leave room for state and
recovery records, and recreates it after space recovers. Low space marks the
agent degraded while it remains available for controller contact.

The signed package also installs the static recipe-build egress proxy. After an
upgrade, the agent advertises `recipe.build.egress-proxy.v1` only when that
root-owned executable is present and safe. The Controller will not send a
public-network recipe build to an older or incomplete installation; no manual
Spark networking or SSH step is needed.

Rerun the channel command without `--enroll` for a local package repair or when
the controller-managed path is unavailable. It preserves configuration and
identity, verifies the installed package version, architecture, and agent binary
against the accepted signed package, installs only when they differ, restarts the
services, and requires sustained readiness. APT indexes are refreshed only if
the package install cannot complete with the indexes already present. There is
no A/B slot, supervisor, rollback state, migration command, or follow-up setup
step.

Healthy connected agents rotate their 30-day client certificate before it
expires; operators should not normally need to re-enroll them. A package upgrade
never mints a replacement identity on its own. If an agent missed rotation during
an outage, or an issued staged generation expired before activation, use Fleet's
**Re-enroll Spark** action and run its generated `--enroll` command. Package
configuration remains local and recoverable even while the expired identity is
offline; sustained readiness still fails until re-enrollment succeeds.

An ordinary rerun is only an upgrade. Enrollment commands use the generic
`--enroll` intent through the signed channel bootstrap. On a fresh Spark that
creates the identity; on an existing Spark the controller-authorized grant
automatically replaces the certificate. If pairing succeeded but readiness did
not, rerun the channel command without `--enroll` to resume recovery without
consuming another token. The generated
URL is `/dev/spark` for a development NAS and `/spark` for a stable NAS.

If the command fails, read its final error and retry after correcting that
condition. A readiness failure preserves pairing; retry without `--enroll`.
Release verification failures stop before privilege escalation.
