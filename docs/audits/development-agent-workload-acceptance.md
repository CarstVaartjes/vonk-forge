# Development agent workload acceptance record

Date: 2026-08-13

Status: accepted. Repository, publication, NAS, both physical Sparks, synthetic
and real-model lifecycles, two-rank failure/recovery, restart persistence,
normal cleanup, public documentation, and temporary-access removal passed.

Private evidence JSON, tokens, qualification inputs, certificates, keys, model
contents, and command output containing host-specific secrets remain in the
operator's mode-`0600` evidence directory. This committed record contains only
public artifact identities, certificate fingerprints, redacted evidence
digests, bounded operation identities, workflow links, and pass/fail outcomes.

## Accepted publication

The persistent rendezvous correction passed an operator-retained independent
agent review, then landed through
[PR #128](https://github.com/CarstVaartjes/vonk-forge/pull/128), merged as
`72e59f04c678e03261b4bedca6b509eafb6ddf07`, and passed every required PR
check. The detailed review transcript remains in the private operator session;
it is not represented as GitHub review metadata. Publication then completed
only from accepted `main`:

- [Development images run 31645030311](https://github.com/CarstVaartjes/vonk-forge/actions/runs/31645030311)
  built, smoke-tested, scanned, attested, and published the exact API and worker
  archives before advancing `:dev`.
- The accepted API manifest is
  `sha256:f12d269bf2cb915b5d640cfe14710a556eee97938e2a45c0fdea350c687166a8`;
  the worker manifest is
  `sha256:254cc75ede909e0205f7d202c699b01d6eb9749296b5c78d0bfc3ed4ecaca02b`.
- [Rust agent run 31645030289](https://github.com/CarstVaartjes/vonk-forge/actions/runs/31645030289)
  lifecycle-tested, signed, and published
  `0.1.0~dev.92+g72e59f04c678` with package SHA-256
  `301ea87dc04d5bed01db8e679d20d4700b750340d370598f9fc69e57167dddb2`.
  The accepted source change did not alter the agent contract, so the physical
  run retained the already activated capability-fix package rather than
  interrupting a workload for package churn.

## Required evidence matrix

| Gate | Result and retained evidence | Status |
| --- | --- | --- |
| Repository correctness | PR #128 passed Ruff, generated clients, PR contracts, Rust contracts, all four repository shards, all four control shards, Python agent, admin web, catalog/service, and release-policy checks. This final documentation diff also passed all 286 local script tests, whitespace validation, and a bounded secret-pattern scan; it is merged only after its required GitHub checks pass. | Passed |
| Independent review | The runtime correction and this documentation-only finalization each received an operator-retained independent agent review. These are not GitHub review comments; GitHub proves CI and merge. All actionable findings were resolved, and the final re-review found zero Critical or Important issue. This row is the public redacted outcome; detailed transcripts remain in the private operator session. | Passed |
| Accepted publication | Exact `main` commit, image manifests, package digest, and successful workflow URLs are listed above. No branch-built or locally published artifact was deployed. | Passed |
| NAS project | The NAS directory contains exactly `docker-compose.yml` plus `secrets/`. Compose SHA-256 is `f0521b1ce1ccb1ab115857d64005a9a708f1c28ce2fd8e74e09bf63405be54df`; all 14 host secret sources are mode `0600`; API, worker, and migration use distinct read-only secret projections; the five long-running services expose no secret-valued environment variables. Ordered pull/stop/start converged every service healthy on revision `72e59f04`; the final read-only audit found every service up, all health checks green, and zero recent Caddy errors. | Passed |
| Spark identity | Spark 1 is `spk_42a502cc1a5de4c79aea1b6b6d993c74`, certificate SHA-256 `07:17:D4:E4:87:18:1E:9A:2B:60:E3:FA:B2:09:D8:95:A4:DC:15:EC:B1:EF:98:C4:B2:07:57:5B:C8:A2:3D:B6`; Spark 2 is `spk_ec7897d93866091c4249cc7825fb95c7`, certificate SHA-256 `62:C7:06:2B:C6:77:C6:15:88:B6:66:2C:7C:5F:A4:8D:E6:E7:2B:B6:CF:F0:A6:90:5E:AC:A0:BD:FE:1A:D4:9A`. Both run signed package `0.1.0~dev.91+g950e4845c54e`, slot B, binary SHA-256 `8c06e776691d1153564fdb7410de9866ec41b609cb20697aa816f9dd206437e3`, Rust protocol 3, and fresh inventory. Neither the service account nor the human operator retains Docker-group access; a fresh SSH session requires `sudo docker`. | Passed |
| Synthetic lifecycle, pass 1 | Physical run `0bc959ef-3496-4106-abd7-0c2ca7c5b07b` completed all 12 states from inventory through normal uninstall with image `sha256:b77a3294aeedf13093f8b10aed7c33126f722a286a3cfd99ceec6e8d0dbfd9e8`. | Passed |
| Synthetic replay | A second physical run, `8c5d1f4b-5b6e-497a-8f0b-be9a5964ec22`, reused the same immutable source/image identity after control and agent restarts and again completed all 12 states. | Passed |
| Real single-node model | The public DS4 lane qualified against both nodes. Run `eff35b3c-7ed1-4f19-a199-dd2748275be2` built a 2,592,110,592-byte wrapper, performed inference, survived the restart checkpoint, and completed normal stop/withdraw/uninstall. Model files remained separate immutable cache objects. | Passed |
| Real two-node gang | Recipe revision 6 used source bundle `daefe17329880a18fb57c34dc76a42997b4393a4d019ae3ebcc22e42fdc5b69a` and wrapper image `sha256:4dec50e77c98a42e9729416252c81b0e16487ae398f20a767f10dd747bd61aad`. Run `df3f38fe-7ae4-4cbd-96b4-bff1e812940f` had rank 0 publish only `192.168.1.211:8000` and `192.168.100.10:29500`, while rank 1 published only `192.168.100.11:8000`. Evidence included a positive direct-fabric host probe and negative management/public probes: the complete direct-fabric HELLO received its exact acknowledgement, while management-path rendezvous and the rank-1 management port were rejected. The packaged firewall check passed on both nodes, and sole-entrypoint inference passed. | Passed |
| Rank failure/recovery | After exact three-label verification, only rank 1's managed container was stopped while its Rust agent remained active. Fresh evidence withdrew the route with HTTP 503. Starting that same container repeated rendezvous, restored both fresh ranks, republished the route, and produced recovered inference without build/install/cache mutation. The same rank-0 coordinator process remained alive throughout. | Passed |
| Restart persistence | Both A/B supervisors and the NAS project were restarted; NAS used ordered stop/start without a pull and retained the exact API image ID. Fleet evidence advanced to `34f73aad8cabbef6f18ff54062f5baf8be0444758c4a1f77f2b35eea8a343972`; the same run and route produced inference without rebuild or model download. | Passed |
| Normal cleanup | Stop operation `7502e2e4-3b27-420e-baba-09571afa5266` and uninstall operation `228177e6-ff88-48fa-b985-b6cc9e88d403` completed. Both nodes have zero Vonk-managed containers, the endpoint returns HTTP 503, and the exact 86,720,111,488-byte base plus 6,971,241,504-byte drafter cache objects remain on each node. | Passed |
| Temporary sudo removal | After the final host audit and website deployment, `/etc/sudoers.d/vonktemp` and `/etc/sudoers.d/99-vonk-codex-temporary` were removed if present on the NAS and both Sparks. Sudo timestamps were invalidated; fresh `sudo -n true` probes returned `PASSWORD_REQUIRED` on all three hosts. | Passed |

## Failure rules

- Do not skip or manually mark a failed row successful.
- Do not deploy branch-built or locally published release artifacts.
- Do not put runtime secrets in an image, Compose environment value, log, audit
  record, or committed evidence file.
- Do not simulate rank failure by stopping an agent or deleting a container,
  cache, identity, named volume, or model directory.
- Remove temporary unattended sudo only after every physical and publication
  action that needs it, and verify removal even if a later non-host task fails.

Every required row has an observed passing result. Any future rollout creates
a new dated evidence record rather than changing these accepted identities.
