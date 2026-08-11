# Development agent workload acceptance record

Date: 2026-08-11

Status: in progress. This record is intentionally incomplete until accepted
GitHub artifacts, the NAS, and both physical Sparks pass every row below. A
healthy API or a passing repository suite alone is not completion.

Private evidence JSON, tokens, qualification inputs, certificates, keys, model
contents, and command output containing host-specific secrets stay in the
operator's protected evidence directory. This committed record contains only
public artifact identities, redacted digests, check/run URLs, and pass/fail
outcomes.

## Required evidence matrix

| Gate | Reproducible procedure | Required retained evidence | Status |
| --- | --- | --- | --- |
| Repository correctness | Run the Python control, agent, repository, Rust, systemd, supply-chain, documentation, secret-scan, and complete-stack commands from the implementation plan | Exact clean commit plus command summaries | Pending final clean-commit run |
| Independent review | Review the final diff after all fixes and resolve every actionable finding | Reviewer result and zero unresolved threads | Pending |
| Accepted publication | Merge through a pull request; allow only GitHub Actions on accepted `main` to publish `:dev`, development APT, and Compose artifacts | PR, accepted commit, workflow URLs, image/package/artifact digests | Pending |
| NAS project | Follow [Development NAS installation](../runbooks/development-nas-installation.md) using exactly `docker-compose.yml` plus `secrets/`; pull and redeploy in the NAS UI | Compose checksum, selected cohort, service health, secret-boundary checks | Pending |
| Spark identity | Follow [Install the Vonk agent](../operations/install-vonk-agent.md) and [Node onboarding](../runbooks/node-onboarding.md) for both nodes | Stable `spk_` IDs, certificate fingerprints, package versions, fresh inventory | Pending |
| Synthetic lifecycle, pass 1 | Run the `synthetic` phase in [Development agent workloads](../runbooks/development-agent-workloads.md) | Exact build/import/install/start/route/inference/stop/uninstall evidence | Pending |
| Synthetic restart replay | Restart both agents and the NAS stack, then repeat the identical synthetic acceptance | Same identities, retained immutable content, no unnecessary rebuild/redownload | Pending |
| Real single-node model | Qualify the public DS4 lane and run `model-single` | Qualification digest, exact image/artifact identities, inference, restart, stop/uninstall | Pending public DS4 pull and accepted deployment |
| Real two-node gang | Run `model-multinode` with both exact target nodes after applying the direct-fabric-only rendezvous firewall rule | Rank-0 address-specific `MASTER_ADDR:29500:29500` publication, Docker-bridge egress through the declared fabric route, positive direct-fabric host probe and negative management/public probes, bounded local-address HELLO/ack, equal image/artifact-set identities, all-rank readiness, sole entrypoint inference | Pending |
| Rank failure/recovery | Verify the management labels, stop only the selected Docker workload container while its Rust agent remains healthy, then start that same container | Fresh failed-rank snapshot, route withdrawal, fresh recovery snapshot, republication, recovered inference | Pending |
| Restart persistence | Restart both Rust supervisors and the NAS project without changing cohort or deleting state | Advanced freshness, same identities, serving route, inference without rebuild | Pending |
| Normal cleanup | Stop and uninstall through the API; retain only normally reference-counted immutable caches | Terminal operation evidence and no active validation workload | Pending |
| Temporary sudo removal | Remove both temporary sudoers filenames on NAS and Sparks and prove `sudo -n true` fails | `PASSWORD_REQUIRED` on all three hosts | Pending; remove last |

## Failure rules

- Do not skip or manually mark a failed row successful.
- Do not deploy branch-built or locally published release artifacts.
- Do not put runtime secrets in an image, Compose environment value, log, audit
  record, or committed evidence file.
- Do not simulate a rank failure by stopping its agent or deleting a container,
  cache, identity, named volume, or model directory.
- Do not remove temporary unattended sudo until every earlier physical gate is
  complete; remove it even if a later cleanup attempt fails.

The final update to this record must replace every `Pending` status with a
specific result or a truthful blocking reason and must link the accepted public
GitHub evidence without exposing private deployment evidence.
