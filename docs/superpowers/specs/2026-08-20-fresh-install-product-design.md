# Fresh-Install Vonk Forge Product Design

## Goal

Make Vonk Forge a fresh-install product whose complete operator experience is
two commands:

```bash
curl -fsSL https://install.vonkforge.ai/nas | sh
curl -fsSL https://install.vonkforge.ai/spark | sh
```

The NAS command runs on an ordinary workstation and creates a complete,
user-owned directory to drag onto a NAS. The Spark command installs or upgrades
the published Rust agent and guides secure pairing. Neither flow requires a Git
checkout. Development and production run the same topology and behavior; only
their immutable release identities differ.

The development channel exposes the identical no-argument workflows at
`https://install.vonkforge.ai/dev/nas` and
`https://install.vonkforge.ai/dev/spark`. Those URLs select development
artifact identities; they do not introduce flags, follow-up commands, or a
different deployment model.

These are two target-specific single-command installers, not a multi-command
installation sequence. The NAS curl runs on the operator's workstation and
creates the directory that will be uploaded to the NAS. The Spark curl runs on
each Spark and leaves its agent installed, paired, started, and verified. A user
never runs the Spark command to prepare the NAS, never runs the NAS command on
the NAS, and never needs a second setup command after either curl completes.

Each curl invocation completes its entire side of the setup. It may prompt for
required choices and secrets through `/dev/tty`, but it must not finish by
asking the operator to download another installer, change permissions, or run a
second setup command. After the NAS command, the directory is ready to upload;
after the Spark command, the installed agent is paired, running, and verified.

This is a clean-slate design. Remove obsolete compatibility, migration, Python
Spark, Git-authority, built-in CA, A/B slot, and supervisor paths instead of
preserving them.

## Product boundary

### NAS preparation

The stable entry point is:

```bash
curl -fsSL https://install.vonkforge.ai/nas | sh
```

The bootstrap reads interactive answers from `/dev/tty`, so piping the script
does not consume the wizard input. It downloads a versioned setup executable
for the local OS and architecture, verifies it against the selected signed
release manifest, and executes it without `sudo` or Docker.

The public endpoint selects the current accepted stable release. The
development endpoint runs the identical wizard against the accepted development
release. The wizard asks for:

- the NAS LAN address; the controller port remains the canonical `8443`;
- Tailscale OAuth credentials used to maintain the canonical gateway identity;
- external provider credentials that cannot be generated locally;
- whether Hermes should be included; and
- whether each locally owned secret should be generated or imported.

It writes exactly one uploadable directory:

```text
vonk-forge/
├── docker-compose.yaml
├── .env
└── secrets/
```

All Compose paths are relative. Non-secret runtime configuration is baked into
the published images or rendered into Compose. The `secrets/` directory owns
private keys, certificates, passwords, tokens, and the Step CA configuration.
Directories use mode `0700` and files mode `0600` where the filesystem supports
POSIX permissions. Existing values are never silently overwritten.

The command does not contact, mount, or administer the NAS. The operator drags
the directory onto the NAS and starts `docker-compose.yaml` in the NAS Docker
runner. Docker Engine and Compose are host-owned NAS capabilities: Vonk Forge
does not install, pin, configure, or make either version an installer input.
Running the command later against an existing local directory preserves site
identity and secrets while replacing only release-controlled values.

### Spark installation and upgrade

The stable entry point is:

```bash
curl -fsSL https://install.vonkforge.ai/spark | sh
```

The bootstrap detects Linux architecture, resolves the selected immutable Rust
package, downloads it as the calling user, verifies its checksum and release
signature, and only then invokes `sudo` for package installation. `curl` itself
never runs as root.

On a fresh Spark, the installer prompts through `/dev/tty` for the enrollment
endpoint, the controller CA SHA-256 shown by the authenticated Fleet workflow,
and a short-lived pairing token without placing the token in process arguments.
Creating that single-use token is the administrator's approval; a valid
submission immediately issues the certificate. The installer fetches the
public bootstrap document without presenting the token, verifies its trust root
against the out-of-band fingerprint, installs a single agent binary and direct
systemd service, and materializes the mTLS identity and configuration
atomically.

On an existing Spark, the same command performs an in-place package upgrade,
restarts the direct service, and verifies both local health and the version
reported to the controller. There are no slots, supervisor, generations,
rollback state, or hidden fallback binary.

## Canonical runtime topology

One canonical Compose model contains:

- PostgreSQL;
- the control API and worker;
- Step CA;
- LiteLLM;
- Caddy;
- the Tailscale gateway and reconciler;
- the private registry;
- Prometheus and Grafana; and
- optional Hermes under the sole optional profile.

There are no development overlays that add or remove services. A release
manifest supplies immutable image references for every service. Development
and production manifests may name different versions, but render the same
services, networks, volumes, commands, healthchecks, and security settings.

All default services are long-running and have meaningful healthchecks. There
are no one-shot Compose services and no containers kept alive by sleeping after
initialization. Initialization belongs to the service that owns the state:

- PostgreSQL's native empty-cluster initialization creates the control and
  LiteLLM roles and databases;
- the control API acquires a PostgreSQL advisory lock, applies the current fresh
  schema, initializes the authority head, and then serves requests;
- each image prepares only its own writable state before dropping privileges
  and executing its long-running process; and
- dependants wait on health, not on container creation or successful exit.

PostgreSQL is the source of truth for control authority, proposals, catalog,
jobs, fleet state, and audit evidence. Runtime Git code, repository mounts,
Git keys, and Git-shaped identifiers are absent.

## PKI and access

Step CA is the only controller and agent PKI implementation. The bundle wizard
creates a coherent root, intermediate, provisioner, controller certificate,
and private material or imports a complete coherent set. Caddy explicitly
serves the generated controller certificate on LAN agent and enrollment names;
it never silently creates an unrelated local CA.

Tailscale terminates browser TLS for the `.ts.net` hostname and proxies to the
internal web ingress. Agent endpoints continue to require the controller trust
root and mTLS where applicable. The browser, enrollment, agent, registry,
LiteLLM, Grafana, and Prometheus routes are tested independently.

## Release authority

GitHub Actions publishes an immutable release manifest containing:

- release channel and version;
- source commit;
- exact image references and digests;
- exact Debian package URL, architecture, version, and digest;
- NAS setup executable identities;
- the canonical Compose template digest; and
- checksums, signatures, provenance, and expiry metadata.

Both curl entry points are small HTTPS bootstraps. They resolve the requested
channel, download the manifest and artifact, verify release metadata before
execution, and fail closed on mismatches. Stable releases promote already
accepted artifacts rather than rebuilding them. Development and production
therefore differ only in selected immutable artifact identities.

The four public endpoint clients are release-independent and immutable once
published. Each channel advances through one signed, expiring manifest object
that names both immutable NAS and Spark bootstraps. Updating that one object is
the only mutable publication operation, so readers cannot observe a mixed NAS
and Spark generation. The immutable release JSON also carries a detached
signature, and stable publication refuses semantic-version rollback.

Publication is receipt-driven: tested artifacts are published first, a complete
manifest is assembled from their accepted receipts, the manifest is verified,
and only then is the channel pointer advanced atomically. Branch protection
requires one fail-closed aggregate covering all behavioral suites.

Every private release-signing key must have a recoverable human-controlled
backup, with 1Password available as an optional destination rather than a
product or CI dependency. Protected GitHub Actions environments may retain
controlled execution copies. A separately encrypted offline escrow must have a
derived public fingerprint matching the tracked verification key; escrow never
reaches CI. The current GitHub-only key remains untouched until a replacement
exists in verified recoverable backup and escrow copies and succeeds in
end-to-end sign/verify testing. Public verification keys and fingerprints
remain tracked, non-secret release inputs. CI does not add a 1Password/OIDC
runtime dependency.

## Testing and acceptance

Tests assert behavior and rendered models, not wording in documentation or
workflow source. Required acceptance includes:

1. Real PostgreSQL authority and transaction tests run against PostgreSQL, not
   SQLite emulation.
2. The canonical Compose model contains the same topology for development and
   production and has no legacy service, overlay, mutable image, absolute path,
   one-shot container, Docker socket, SSH exposure, or repository dependency.
3. Generated default and Hermes YAML passes an officially downloaded,
   checksum-verified UGREEN compatibility fixture (Docker 29.4.3 / Compose
   5.1.3) and a declared lower Compose fixture. Those fixture versions are CI
   compatibility inputs only. One clean reference-runner rollout starts from
   empty volumes with no warnings, every selected service becomes healthy, and
   no service is exited.
4. LiteLLM has a distinct initialized role and database.
5. Caddy serves the configured controller certificate and Tailscale browser
   access works at the advertised `.ts.net` URL.
6. The NAS command runs without Docker, root, Git, SSH, or NAS access and emits
   the exact three-item directory contract without bundled secret values.
7. The Spark command installs on amd64 and arm64, pairs a real packaged agent
   through Caddy/API/Step CA, executes a job, renews identity, and upgrades the
   package directly.
8. Hermes is absent by default and becomes one healthy long-running service
   when selected, without any setup container.
9. Required CI jobs cannot pass through skips, placeholders, mutable aliases,
   missing artifacts, or untested publication inputs.

## Removal scope

Delete, rather than deprecate:

- Python Spark/node agent and migration code;
- A/B slots, supervisor crate, supervisor service, activation state, rollback,
  and controller slot/generation protocol fields;
- the runtime update signer, agent-update TUF publication, and controller-driven
  agent update or rollback operations;
- runtime Git repository, proposal, code-host, and signing paths;
- built-in/local CA deployment choices and flat credential fallbacks;
- legacy enrollment service implementations superseded by the Rust mTLS flow;
- one-shot Compose setup services and fake persistent bootstrap services;
- migration/rollback documentation for deployment models that never shipped;
  and
- tests that only enforce documentation text or removed compatibility behavior.

Git history is retained.
