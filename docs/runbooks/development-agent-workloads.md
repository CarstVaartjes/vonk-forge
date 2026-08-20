# Development workload acceptance

This runbook verifies an accepted development release on a real NAS and one or
more NVIDIA DGX Spark nodes. Development uses the production topology and
behavior; only the immutable release identities differ.

## Install the accepted development release

Prepare the NAS upload directory on a Linux or macOS workstation:

```sh
curl -fsSL https://install.vonkforge.ai/dev/nas | sh
```

Upload the generated `vonk-forge/` directory to the NAS and start it as one
Compose project. Do not add source checkouts, host paths, alternate ports,
manual secret files, or helper containers.

For every Spark, create a distinct one-use pairing token in Fleet and run on
that Spark:

```sh
curl -fsSL https://install.vonkforge.ai/dev/spark | sh
```

Enter the enrollment URL, controller CA fingerprint, and token shown by Fleet.
The installer discovers the controller endpoint and CA, verifies the CA against
the out-of-band fingerprint, installs the direct Rust agent, pairs it, starts
the service, and requires sustained readiness. There is no separate APT setup,
manual CA copy, SSH bootstrap, approval queue, or second pairing invocation.

## Physical prerequisites

Before workload acceptance, require:

- the NAS Compose project is healthy and its immutable images match one
  accepted development generation;
- each Spark runs the accepted agent version and reports a fresh,
  certificate-bound `spk_` identity;
- NVIDIA driver, Docker, NVIDIA Container Toolkit/CDI, rootless Podman, local
  storage, and the agent's systemd user manager pass the packaged self-test;
- every multi-node participant reports the intended non-management fabric
  address and measured reachability; and
- no mutable tag, local image, repository checkout, shared credential, or
  manually edited `agent.toml` participates in the acceptance.

Hostnames and IP addresses are observations. The certificate-bound `spk_`
value is node identity.

## Acceptance sequence

Run one node at a time until the synthetic lifecycle is green, then widen to
the intended topology:

1. Confirm Fleet inventory is fresh and the planned resource reservation fits.
2. Import or create an immutable recipe revision.
3. Preview installation and placement on the selected Spark set.
4. Install and require exact build, transfer, import, and runtime evidence.
5. Start the recipe and require route publication only after the workload is
   ready.
6. Send a bounded inference request through the normal private Tailscale URL.
7. Stop the run and require route withdrawal.
8. Uninstall and confirm the recipe-owned runtime resources are gone while
   shared immutable caches remain reference-counted.

For multi-node recipes, also prove rank failure withdraws the route, recovery
does not silently change the accepted recipe or artifact digest, and a full NAS
or Spark restart restores only persisted authority—not stale readiness.

The contributor harness `scripts/run-development-slices` automates the same API
lifecycle when authenticated test endpoints are deliberately available. It is
acceptance tooling, not an installation path; it never generates deployment
secrets, publishes a NAS project, installs an agent, or replaces the two curl
commands above.

## Upgrade and recovery

Prepare a NAS upgrade by rerunning the development NAS command from the parent
of the existing local `vonk-forge/` directory. Upload it and redeploy while
keeping named volumes. Upgrade a Spark by rerunning the development Spark
command on that node. Verify one canary before continuing through the fleet.

If this pre-release deployment is intentionally disposable, a full Compose
volume reset is permitted only as an explicit fresh-install acceptance step.
Normal upgrades keep PostgreSQL, Step CA, Tailscale, and application volumes.
There is no schema downgrade, prototype migration, A/B rollback, or legacy
state import path.

## Evidence

Record only public or bounded evidence: release source SHA, image/package
digests, certificate fingerprints, node IDs, operation IDs, state transitions,
and response hashes. Never capture pairing tokens, OAuth credentials, provider
keys, private keys, database URLs, session cookies, or complete secret files in
terminal logs, CI artifacts, screenshots, or issues.
