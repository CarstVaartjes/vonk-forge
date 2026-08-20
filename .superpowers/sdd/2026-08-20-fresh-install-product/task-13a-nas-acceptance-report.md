# Task 13A NAS Acceptance Report

**Status:** DONE

## Commits and scoped files

Task 13A implementation and fix-round commits in scope:

- `1df4b496` — behavioral candidate acceptance foundation
- `123a7d34` — initial acceptance evidence
- `b0311e45` — NAS acceptance hardening
- `c77f3342` — initial route and gate-authority coverage
- `c46a0908` — corrected current-state report
- `d6813094dd1799d4b40357af9586b5e0312865b2` — routed-service, tunnel-exit,
  DIND-fixture, and candidate-receipt fixes
- `fd25eb1bc8056661a0087950d45e7ed333a7c749` — exact structured Tailnet Serve
  status and upstream-map validation

This evidence update is committed separately. The implementation commit changes
only `.github/workflows/installer-publication.yml`, the NAS acceptance runner,
its transport runtime, and their focused tests. Unrelated worktree edits remain
unstaged.

## Current requirements mapping

| Requirement | Current evidence |
| --- | --- |
| Candidate before promotion | The parsed workflow keeps immutable candidate publication before NAS/Spark acceptance, signed aggregate acceptance, and channel-pointer promotion. The candidate receipt artifact now uploads both the immutable bundle and `candidate-receipt.jsonl`. |
| NAS Docker boundary | The installer does not install, pin, configure, or require NAS Docker/Compose. Docker Engine 29.4.3 and Compose 5.1.3 are CI-only compatibility fixtures. |
| YAML compatibility | Generated YAML omits top-level `name` and `version`, has no include/build/host-bind input, uses digest-pinned images, and rejects every parser diagnostic. |
| One rollout and compatibility fixtures | Verified Compose v5.1.3 and v2.24.6 run config-only compatibility checks. Exactly one empty-volume Hermes-superset rollout remains; default topology is config-validated. |
| Executable DIND fixture | The workflow manually starts the digest-pinned Docker 29.4.3 DIND daemon, publishes only `127.0.0.1:2375`, mounts `GITHUB_WORKSPACE` at the identical absolute path, waits for and requires server version `29.4.3`, propagates its inspected IPv4 for candidate Caddy routing, and always removes the daemon. Candidate bundles are generated under the shared workspace. |
| Bundle and workstation contract | The constrained PTY path, exact three-item output, POSIX modes, repeatability, secret isolation, and site-secret preservation remain asserted. |
| Routed LiteLLM and database behavior | The advertised Tailscale/Caddy route accepts authenticated `/v1/models` only with the generated LiteLLM key and returns JSON model data. Missing and wrong credentials must return 401/403. The PostgreSQL check also requires a nonempty initialized LiteLLM public schema. |
| Routed Prometheus and Grafana behavior | Authenticated traffic through the advertised Tailscale/Caddy Grafana route verifies the administrator API, provisioned Prometheus datasource, both dashboards, and a successful `up{job="vonk-control"}` query through Grafana’s configured datasource proxy. Missing and wrong Grafana credentials must return 401/403. |
| Routed registry and tailnet behavior | The configured external Caddy registry SNI `/v2/` rejects no-client-certificate traffic and returns exactly `{}` to a short-lived client certificate chained to the candidate CA. Tailscale acceptance parses `serve status --json` and requires the exact selected HTTPS listener object, then parses `serve get-config --all` and requires the exact selected service-to-upstream map; duplicate, extra, missing, wrong-port, wrong-protocol, and wrong-target data fail closed. Successful LiteLLM/Grafana checks traverse the advertised `svc:vonk-forge` HTTPS route. |
| PTY/tunnel hardening | Partial writes and TLS WANT_READ/WANT_WRITE handling remain. The HTTPS-over-command helper now closes stdin, waits for the tunnel child, and requires exit status zero after a successful HTTP response; cleanup cannot mask the primary failure. |
| Gate authority and custody | Parsed workflow NAS gates feed the real acceptance authority and gate drift fails closed. Protected GitHub environment execution copies remain; no GitHub/1Password secret or OIDC runtime dependency changed. |

## Red/green evidence

Initial focused red command:

```text
$ uv run pytest tests/test_acceptance_runtime.py::test_https_tunnel_rejects_a_successful_response_from_a_failing_child tests/test_fresh_nas_acceptance.py::test_routed_service_checks_require_authentication_and_expected_data tests/test_installer_publication_workflow.py::test_nas_dind_fixture_starts_a_shared_loopback_daemon_and_fails_wrong_version tests/test_installer_publication_workflow.py::test_nas_dind_fixture_is_always_removed_and_candidate_receipt_is_uploaded -q
FFFF
4 failed in 0.29s
```

The failures proved that a successful HTTP response masked tunnel exit 9, routed
service acceptance did not exist, and the workflow lacked manual DIND start and
always-run cleanup. The additional DIND-address regression was observed before
implementation:

```text
$ uv run pytest tests/test_installer_publication_workflow.py::test_nas_dind_fixture_starts_a_shared_loopback_daemon_and_fails_wrong_version -q
F
1 failed in 0.07s
```

It required the nested daemon’s inspected IPv4 to reach the candidate Caddy
route rather than incorrectly using the runner host address.

Focused green regression command:

```text
$ uv run pytest tests/test_installer_publication_workflow.py::test_nas_dind_fixture_starts_a_shared_loopback_daemon_and_fails_wrong_version tests/test_installer_publication_workflow.py::test_nas_dind_fixture_is_always_removed_and_candidate_receipt_is_uploaded tests/test_acceptance_runtime.py::test_https_tunnel_rejects_a_successful_response_from_a_failing_child tests/test_fresh_nas_acceptance.py::test_routed_service_checks_require_authentication_and_expected_data -q
....
4 passed in 0.40s
```

Tailnet Serve parser red/green evidence:

```text
$ uv run pytest tests/test_fresh_nas_acceptance.py::test_tailnet_serve_status_requires_the_exact_selected_routes -q
F
1 failed in 0.06s

$ uv run pytest tests/test_fresh_nas_acceptance.py::test_tailnet_serve_configuration_requires_exact_selected_upstreams -q
F
1 failed in 0.07s

$ uv run pytest tests/test_fresh_nas_acceptance.py::test_tailnet_serve_status_requires_the_exact_selected_routes tests/test_fresh_nas_acceptance.py::test_tailnet_serve_configuration_requires_exact_selected_upstreams -q
..
2 passed in 0.03s
```

The first red test established that no structured status assertion existed. The
second established that acceptance did not obtain or compare `serve get-config
--all`. The final test accepts only the complete selected default/Hermes Serve
objects and rejects extra/missing services and routes, wrong upstream targets,
wrong ports/protocols, node listeners, and duplicate JSON keys.

## Final verification

```text
$ uv run pytest tests/test_acceptance_runtime.py tests/test_fresh_nas_acceptance.py tests/scripts/test_build_nas_compose_bundle.py tests/scripts/test_install_release_publication.py tests/test_installer_publication_workflow.py -q
......................................................                   [100%]
54 passed in 11.38s

$ uvx --from ruff==0.16.1 ruff check --force-exclude tests/acceptance/test_fresh_nas_install.py tests/test_fresh_nas_acceptance.py
All checks passed!

$ uv run python -c '<parse workflow; bash -n DIND/start/acceptance/cleanup steps>'
workflow NAS shell syntax: ok

$ git diff --check
(exit 0; no output)
```

## CI-only/full-rollout gap and self-review

The protected candidate curl, R2 publication, canary secrets, Tailscale
registration, actual Docker 29.4.3 DIND daemon, and the one full clean rollout
are CI-only and were not run locally. The workflow is executable and
fail-closed: DIND readiness requires the exact server version, reports are
emitted only after acceptance, aggregate signing requires the canonical gate
set, and promotion is downstream.

Self-review found and corrected the nested-DIND route-address issue: the
candidate Caddy port belongs to the DIND network namespace, so its inspected
IPv4 is now the acceptance NAS address. No installer, bundle, `.env`, secret,
prompt, or deployment input contains Docker/Compose fixture versions. External
Recoverable replacement-key backup and fingerprint-verified encrypted offline
escrow provisioning remain required; 1Password is optional rather than a CI or
product dependency. The current GitHub-only key was untouched.

## Superseded historical record

Earlier report revisions that described generated top-level `name`/`version`
fields or an allowed Compose-v5 warning were intermediate defects, not current
behavior. The mapping and verification above supersede those statements.
