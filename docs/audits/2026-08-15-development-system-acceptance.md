# Development system acceptance record

Date: 2026-08-15

Status: accepted. Publication, signed agent rollout, NAS redeployment,
two-Spark runtime, restricted client access, public browser access, public
website deployment, normal cleanup, and temporary-access removal passed.

This record intentionally excludes passwords, API keys, private keys, session
cookies, certificate bodies, model contents, and unredacted host state. Private
source material remains in the operator's encrypted 1Password vault and
mode-`0600` local source generation; generated runtime projections remain on
the NAS only.

## Accepted publication

The accepted source is `main` commit
`28906648db8a1cffdcc52d9193f86610143e39ed`. It includes the bounded fast
recipe-uninstall correction from
[PR #160](https://github.com/CarstVaartjes/vonk-forge/pull/160) and the
canonical NAS `docker-compose.yaml` publisher from
[PR #161](https://github.com/CarstVaartjes/vonk-forge/pull/161).

- [Development images run 31870712772](https://github.com/CarstVaartjes/vonk-forge/actions/runs/31870712772)
  built, lifecycle-smoked, secret-scanned, attested, and published the exact
  accepted API and worker images before advancing `:dev`. The API OCI index is
  `sha256:9c38e6e5d40c01105dea3bf61e77aad6d38e9f2b1187717d9f82c7af82d35f4e`;
  the worker OCI index is
  `sha256:2636bc548408354d1447e861ba1a593e825e4fc3f37f40524c479b167be26c68`.
  Independent `gh attestation verify` checks bound both indexes to this exact
  `main` commit and workflow.
- The accepted mutable Compose artifact is
  `vonk-forge-dev-compose-28906648db8a1cffdcc52d9193f86610143e39ed`.
  Its development file SHA-256 is
  `b1f42f5415005cf58525950d609a0064f612978907312d2a7f6f0b7182bfd2de`;
  the companion pinned file SHA-256 is
  `3be36a09a8de5cf33cdc0706baed0126cd32983a0fb2f98e8990fa207663fe1c`.
- [Rust agent run 31870712777](https://github.com/CarstVaartjes/vonk-forge/actions/runs/31870712777)
  lifecycle-tested, signed, attested, and published
  `0.1.0~dev.124+g28906648db8a`. The package SHA-256 is
  `a5c19b418b856b787b2d9f1b3892c7428116bff1c69a9ad392a6796cb5ab9267`;
  its checksum and GitHub attestation independently verified against the exact
  accepted source and release workflow.

## Acceptance matrix

| Gate | Result and retained evidence | Status |
| --- | --- | --- |
| NAS project shape | The remote publisher atomically produced exactly `docker-compose.yaml` plus `secrets/`. The Compose SHA-256 is `e76c6fd8b811044ce0cc2b7c2a11f6f06a425439a5c3b18098289f72e1500428`; `docker compose config --quiet` passed. Twenty-two private source inputs projected to 18 least-privilege runtime files, all mode `0600`. No image contains a runtime secret. | Passed |
| Final NAS cohort | Pull/redeploy converged API, worker, local `main`, and `origin/main` to exact revision `28906648db8a1cffdcc52d9193f86610143e39ed`. Repository initialization, cohort initialization, migration, browser-auth initialization, LiteLLM database initialization, and supervisor initialization all exited 0. PostgreSQL, API, LiteLLM, Caddy, Tailscale gateway, and Tailscale configurator were healthy; the worker was running. Named volumes and the running MIA workload were preserved. | Passed |
| Browser access | The stable private Tailscale HTTPS Service `https://vonk-forge.tail46101a.ts.net/` returned HTTP 200 from a Tailscale-connected Windows client and from the final verification host. The gateway reports online with no key expiry, so it is not ephemeral. Application login as exact subject `admin` succeeded; tailnet membership remains only the network gate. | Passed |
| Caddy and ingress | The private HTTPS Service returned HTTP 200 after redeploy. Caddy, the gateway, and the configurator were healthy, and Caddy reported zero errors in the final three-minute window. The gateway remained online with `KeyExpiry=null`; Funnel remained unnecessary and disabled. | Passed |
| Spark agent package | Both physical Sparks run signed package `0.1.0~dev.124+g28906648db8a`, active slot A, binary SHA-256 `8dff952221e338427b9ddb267e2e7c514825ea8709a6a2af1cf9ea73d9c8deb1`, Rust protocol 3, and migration state `complete`. Both supervisors converged `stable`, reported `rollback_performed=false`, and retained active services. The controller observed both nodes active, non-stale, and less than one second old after rollout. | Passed |
| Current MIA source | The accepted recipe uses upstream `MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark` commit `f752cd04ab30f2cf42077dd8811a5e1e682d63e7`. A fresh remote lookup showed both upstream `HEAD` and `main` at that exact commit. | Passed |
| Current two-Spark workload | Both ranks retained the current installation and managed container through both A/B activations. The accepted wrapper image is `sha256:8904f9ddf1f30556e4429b9e149a959c3873767e1233f3cb166d55306fd61e66`; shared 155 GiB model caches were not rebuilt or removed. Post-rollout inference through LiteLLM model `mia-deepseek-v4-flash` returned HTTP 200, a normal `stop`, and non-empty content. | Passed |
| Stale recipe cleanup | All 20 obsolete recipe installations are uninstalled; only the current installation remains. Exact old, unreferenced `recipe-build-*` tags were removed without force. With the accepted uninstall contract, final stale removals completed in 0.6-0.7 seconds instead of re-hashing the shared model cache. | Passed |
| Pi/LiteLLM key boundary | The restricted key is stored as **Vonk Forge Pi/LiteLLM API Key** in the operator's Private 1Password vault. A freshly projected copy completed `mia-deepseek-v4-flash` inference with HTTP 200 and non-empty content, while `GET /key/list` returned HTTP 403. The temporary local copy was securely overwritten and removed immediately after verification. | Passed |
| Public website and install guidance | `https://vonkforge.ai/`, `/architecture`, and `/install` returned HTTP 200. [Web PR #36](https://github.com/CarstVaartjes/vonk-forge-web/pull/36) corrected the final canonical filename and merged as `94831ca7c8d96378a1d01827f1b6141c9641f616`; [CI](https://github.com/CarstVaartjes/vonk-forge-web/actions/runs/31871796042) and the [Cloudflare Pages deployment](https://github.com/CarstVaartjes/vonk-forge-web/actions/runs/31871796027) passed. The live bundle contains `docker-compose.yaml + secrets/` and no stale `.yml` variant. The public architecture explains one, two, or many Sparks; the control/runtime authority split; secret placement; and the generic Docker UI installation. | Passed |
| Temporary material and unattended sudo | Ten obsolete local staging trees were removed while the current NAS secret generation, MIA evidence, and Tailscale source material were preserved. The temporary Pi key was securely removed. `/etc/sudoers.d/vonktemp` and `/etc/sudoers.d/99-vonk-codex-temporary` were removed if present on the NAS and both Sparks; invalidated `sudo -n true` probes returned `PASSWORD_REQUIRED` on all three. The exact API and LiteLLM SSH tunnel processes were terminated and ports 18080 and 14000 were no longer listening. | Passed |

## Normal operator path

For a fresh development installation, follow
[Fresh development installation](../runbooks/fresh-development-install.md).
Generate runtime secrets outside the images, publish the accepted development
artifact with the guarded remote publisher, and retain only
`docker-compose.yaml` plus `secrets/` in the NAS project. Normal development
updates leave that file unchanged: select it in the generic Docker UI, choose
**Pull**, then **Redeploy**, and preserve all named volumes. Sparks install only
the signed `dev` APT package and activate updates canary-first through the A/B
supervisor.

Production remains a separate trust path. Mutable `:latest` is discovery and
evaluation metadata; production selection, migration, rollback, and exact
image identity stay behind the trusted host updater and release tags.

## Failure rules

- Do not mark any pending or failed row successful without fresh evidence.
- Do not publish branch-built, local, unsigned, or unattested artifacts.
- Do not copy runtime secrets into an image, Compose environment value, log,
  committed audit, or public website.
- Do not delete named volumes during a normal mutable `:dev` update.
- Do not bypass A/B activation or continue to Spark 2 after a failed canary.
- Remove temporary unattended sudo and local tunnels only after every physical
  verification is complete, then prove non-interactive sudo is denied.

Future accepted rollouts create a new dated record rather than rewriting these
artifact identities.
