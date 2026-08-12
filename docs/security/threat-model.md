# Vonk Forge platform threat model

## Security objectives

The platform must not publish an unhealthy model, execute untrusted recipe
instructions, confuse a mutable address with physical node identity, disclose
prompts or credentials, or permit an unreviewed state to reach the cluster.
Authority is deliberately split: local PostgreSQL is authoritative for recipe
catalog entries, immutable recipe revisions, imports, installations, and runs;
Git/TUF remains authoritative for platform source, fleet policy, and the
existing workload-release projection. The global catalog is a publisher and
distribution service, not a runtime dependency.

## Trust boundaries

| Boundary | Assets and attacker | Prevention | Detection and recovery | Executable evidence |
|---|---|---|---|---|
| Tailnet human ingress | Sessions, inference keys; unauthorized tailnet or LAN client | Tagged userspace Tailscale gateway, separately granted exact named Services, no human LAN listener, private gateway-to-Caddy network, Caddy body/header and API auth controls | Tailnet policy tests and bounded API logs; revoke user, gateway node, tag, or Service and withdraw routes | `deploy/compose/tests/test_tailscale.py`, `test_networking.py`, `control/tests/test_api.py` |
| Restricted GPU node LAN ingress | Enrollment grant, agent identity, registry artifacts; hostile LAN client | One NAS-IP-bound backend port, firewall management-CIDR restriction, route-minimal SNI split, mTLS for agent/registry, no human routes | Caddy/control audit, certificate revocation, enrollment expiry, firewall review | `deploy/compose/tests/test_agent_ingress.py`, `control/tests/security/test_agent_identity.py` |
| Admin browser | Proposal intent; malicious site or compromised browser | Same-origin API only, SameSite/HttpOnly session, double-submit CSRF, typed forms, explicit diff confirmation | Audit request/actor/base/targets; revoke session | web unit/Playwright tests, authorization matrix |
| CLI token | Administrator capability; local unprivileged user | HTTPS origin validation, regular non-symlink token file, bounded JSON, no token in argv/output | API audit and token rotation | `tests/cluster_profiles/test_control_client.py` |
| Git/code host | Desired state; malicious contributor or remote | Full immutable commit IDs, protected-branch reachability, exact required checks, signed commits, one-way PR-only release policy | Proposal/commit digests and CI; revert through reviewed PR | repository/proposal/git-policy/reconcile tests |
| Repository content | API parsers and planner; malicious committed files | Repository is mounted only by the API; allowlisted roots, object reads, no hooks/protocols, blob/size checks, canonical typed serializers, local-only endpoints, and immutable adapter executable paths | Validation results and rejected proposal audit | `control/tests/security/test_untrusted_repository.py`, `test_boundaries.py` |
| PostgreSQL | Local recipe catalog, jobs, sessions, audit; database attacker or accidental misuse | Private data network, file secrets, migrations, immutable revision checks, operation fences, and no remote catalog requirement for local execution | Health alert, encrypted backup, audit/count verification; restore must preserve revision/content digests | migration, catalog, job, backup/recovery tests |
| Control worker | Cluster mutation; forged reconciliation, stale/crashed worker, internal response spoofing | Production rejects generic job submission and quarantines legacy unlinked rows without executing an attempt. The worker has a distinct image with no Git/OpenSSH, repository/key mount, or GPU node network; it consumes authenticated persisted plans and nonce-bound, short-lived HMAC repository decisions over a dedicated two-party internal network. Transactional locks, leases, attempt fences, and atomic route acknowledgements fence every effect | Worker-starvation/lease alerts and durable operation evidence; fail-closed withdrawal, compensation, or operator review | production-worker, worker-authority, reconciliation, PostgreSQL race, Compose, and built-image boundary tests |
| Agent enrollment and identity | Agent impersonation, enrollment replay, stolen certificate | Caddy mTLS accepts a 24-hour client-auth-only certificate for one canonical node; enrollment grants are hashed, node-bound, single-use, and short-lived; Smallstep JWK authorization is one-use and fixed-policy | Local PostgreSQL revocation denies immediately; retry only unconfirmed Smallstep serials; certificate loss requires console-verified re-enrollment | `control/tests/test_step_ca.py`, `tests/runbooks/test_agent_pki.py` |
| Agent CA boundary | Online issuer compromise, root theft, forged provider response | The offline root private key is never mounted; step-ca gets encrypted intermediate material and public provisioner JWK, while control-api alone gets the private JWK; fixed URL/root, bounded TLS HTTP, exact CSR/certificate/chain validation | Rotate the online intermediate/provisioner, revoke affected nodes, preserve local denial during remote uncertainty, restore CA DB and PostgreSQL from one backup generation | `control/tests/test_step_ca.py`, `deploy/compose/tests/test_agent_ingress.py` |
| Control-to-agent operation protocol | Cross-node claim, stale fence, malicious payload | A versioned shared wheel accepts only allowlisted operations; every claim binds job, node, attempt, fence, commit, digest, and UTC deadline. Reject unknown fields, commands, arbitrary filesystem paths, credentials, and documents over 64 KiB; slash-bearing result evidence is limited to field-specific endpoint and immutable model-identity grammars | Persist bounded progress/result evidence; reject expired or superseded fences, mark the operation for retry/operator review, and retain the prior attempt for audit | `control/tests/security/test_agent_protocol.py`, `agent_protocol/tests/test_contracts.py` |
| Spark Docker/NVIDIA runtime | Root-equivalent Docker socket, malicious recipe image, confused retry, arbitrary GPU/host access | The agent cannot read the Docker socket and is never in the Docker group. The API-only Ed25519 authority signs an expiring grant bound to node/job/operation/attempt/fence/action and a canonical request digest. A root helper verifies the local request and imported image receipt, then compiles fixed Docker flags: numeric non-root user, read-only root, init, no launch-time pull, bounded rotating local logs, all capabilities dropped, no-new-privileges, bounded PID/memory/swap/shared-memory, explicitly address-bound declared ports, managed mounts, and optional `--gpus all`. Host networking, privileged mode, arbitrary devices/mounts, raw InfiniBand, wildcard/loopback publications, and socket mounts are rejected | Exact retry uses a canonical container label; conflicting or stopped leftovers fail closed. Stop is idempotent, normal cleanup removes the container, and admission/route publication still require accepted operation evidence | `rust/crates/vonk-agent-helper/tests/authority.rs`, `rust/crates/vonk-agent/tests/workloads.rs`, `control/tests/test_host_helper_authority.py` |
| Agent result channel | Result exfiltration or secret-bearing diagnostic output | Result schema applies the same recursive secret/path rejection and 64 KiB limit; control redacts failure reasons before persistence | Treat unexpected result rejection as a security event, rotate exposed credentials, revoke the certificate, and recollect only approved bounded evidence | protocol boundary and logging tests |
| Agent credential storage | Certificate theft from an agent or control host | Store only public certificate metadata in PostgreSQL; private keys remain in protected node-local storage and are never accepted in protocol messages | Revoke the affected serial, quarantine its node, issue a replacement after console identity verification, and review all operations under the stolen serial | agent migration, protocol boundary, and recovery runbooks |
| Agent presence and management address | Spoofed proxy headers, DHCP churn or address reuse, stale observations, and accidental routing over a direct fabric | Caddy deletes every incoming `X-Vonk-Agent-*` value and, only after mTLS verification, supplies the direct peer address plus a private proxy-auth token; middleware converts this to typed scope state. Control binds it to the certificate-authenticated `spk_` ID, requires a canonical address inside `VONK_MANAGEMENT_CIDRS`, excludes `VONK_DIRECT_FABRIC_CIDRS`, and expires observations after 150 seconds | Invalid or stale observations fail closed. An address change publishes maintenance before replacement validation, so the old address is withdrawn and cannot reappear after a rejected replacement | `control/tests/test_presence.py`, `control/tests/test_routes.py`, `control/tests/security/test_agent_identity.py`, `deploy/compose/tests/test_agent_ingress.py` |
| GPU node SSH | Break-glass root policy and onboarding; hostile network/node impostor | Never used by the production API/worker. Explicit operator-only entry points retain strict host keys, no shell interpolation, staged digest checks, and recovery gates | Identity quarantine and resumable journal; console rollback | no-routine-SSH, built-worker-image, install identity/remote/steps tests |
| LiteLLM/Caddy routes | Inference availability; shadow model/upstream; dead publisher | Routes only from an authenticated persisted plan, compatible active agents, fresh policy-bounded management evidence, catalog-revision or repository-release digest, accepted result digests, and the singleton publication owner. Hermes candidates come from exact-commit API policy. The atomic marker and LiteLLM config require one exact supervisor acknowledgement and bounded lease | Route-state and lease-expiry alert; empty bootstrap on worker death, restart, invalid replacement, lost applicable authority, or expired presence | route runtime, reconciliation races, LiteLLM supervisor, and Hermes policy tests |
| Metrics and logs | Operational metadata, prompts/secrets; curious viewer | Stable bounded labels, separate scrape token, centralized redaction/truncation, role-gated content-addressed logs | Secret-leak tests, rotation, checksum verification | metrics/logging/observability tests |
| Backup storage | Database/config/Hermes state copies; backup thief or tampering | Required external authenticated encryption, canonical manifest/checksums, 0600 files, no plaintext production mode; Hermes data/workspaces included and disposable cache omitted | Restore verification before destructive action; Hermes remains stopped pending fresh presence/routes; disposable-host drill | offline and backup/restore tests |
| Docker service host | All services; host admin, disk loss | Separate least-privilege containers, read-only roots, numeric users, private networks, digest-pinned images, bounded volumes/logs | Supply-chain verification, host-loss restore, alerts | Compose and release acceptance gates |
| Tailscale gateway recovery | Human ingress identity; stolen OAuth secret, lost state, or stale extra Service | File-backed OAuth client limited to `auth_keys` for `tag:vonk-gateway`, persisted state, exact Service auto-approvals, exact exported three-Service map, HTTPS-only listeners, no wildcard or LAN fallback | Revoke OAuth client/node/tag, verify status and exported map, restore encrypted state or create one reviewed replacement | `deploy/compose/tests/test_tailscale.py`, Tailscale runbook |
| Hermes Agent | Prompts, sessions, repository credentials and terminal tools; hostile tailnet user, prompt injection, or container escape | Separate tailnet and API identities, read-only root, `no-new-privileges`, exact `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, and `SETUID` supervisor capability allowlist, no host ports/socket/devices/control networks, three exact networks, fixed local LiteLLM alias, explicit CORS | Revoke user/API/repository credentials, stop service, inspect bounded logs, restore encrypted data/workspaces and require fresh routes | `test_hermes_agent.py`, `hermes-agent-runtime.sh`, Hermes runbook |
| Hermes host egress | NAS/GPU node/control services; malicious tool or prompt-driven network access | One-off script resolves the exact bridge and installs an owned source-bound chain denying management, direct-fabric, metadata, and sibling Docker subnets while preserving DNS/Internet | `--verify`, host firewall audit, stop Hermes on drift; no Docker self-repair privilege | `test_hermes_egress.py`, Hermes runbook |

## Role matrix

Viewer is read-only. Operator may preview proposals and plan or enqueue an
eligible reconciliation. Administrator additionally submits repository
changes and performs release-policy transitions. The executable
`MUTATION_ROLES` matrix is required to equal every mutating `/api/v1` route.

Offline bootstrap/recovery is not an API role. It requires host access, an
exclusive lock proving API and worker are stopped, and explicit destructive
confirmation for restore.

## Residual risks

Physical compromise of a GPU node or control host, a malicious signed base image,
and compromise of all protected-branch administrators remain outside software
prevention. Recovery depends on independent console access, off-host encrypted
backups, pinned image/SBOM verification, and protected code-host credentials.
Hardware acceptance is never inferred from simulation and requires explicitly
approved targets.

Hermes intentionally has terminal and Internet tooling. Prompt injection or a
malicious repository can therefore alter its persisted state, disclose a
credential available inside its own container, or act through that credential.
The minimal supervisor Linux-capability allowlist, read-only root, network segmentation,
host egress chain, narrow repository credentials, and encrypted recovery limit
blast radius; they do not make agent-executed code trustworthy. The pinned
image must pass the runtime harness with only `CHOWN` for targeted ownership,
`DAC_OVERRIDE` for the root supervisor to access s6 locks deliberately re-owned
by the runtime user and inspect owner-only binds, `FOWNER` for fixing s6 runtime
permissions, and `SETGID`/`SETUID` for the final unprivileged process transition
before deployment.

An agent security incident has a deliberate recovery boundary: do not reuse an
enrollment secret or certificate after suspected impersonation, replay, theft,
or exfiltration. Quarantine the node, revoke its certificate, invalidate any
running fence, rotate affected credentials, inspect durable attempt evidence,
and re-enroll only after an independent console identity check. A stale or
rejected result is not success evidence; the parent job remains recoverable
through an explicit retry or operator decision.

Smallstep revocation is passive in v0.30.2: it prevents CA renewal but does not
make an already-issued leaf disappear from every TLS verifier. `vonk-forge`'s
database and Caddy-to-control identity validator are therefore the immediate
revocation boundary. A control database outage fails agent authorization
closed. The remaining exposure is bounded by the 24-hour leaf lifetime.

Management addresses remain observations, not cryptographic service identities.
If DHCP reassigns a GPU node address immediately to a hostile LAN host, traffic to
an already-published inference endpoint could reach that host until the
150-second observation window and reconciliation withdraw the route. DHCP
reservations, network admission controls, the upstream application key, and
alerting on address changes reduce this residual risk; hard-coded per-node IPs
would not remove it and are not used as an identity control.
