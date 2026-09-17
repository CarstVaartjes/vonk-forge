# Publish the Vonk Forge installers

GitHub Actions owns package and installer publication. Accepted `main` commits
publish a development release; an accepted `vX.Y.Z` tag promotes already-tested
artifacts to stable without rebuilding them.

One release contains the canonical Compose payload, all digest-pinned runtime
images, native NAS setup executables for supported workstation platforms,
native Spark setup executables for Linux amd64 and arm64, and matching Debian
packages. Publication assembles these only from successful build and acceptance
receipts, verifies the complete signed release manifest, and advances the
channel pointer atomically last.

Operators never configure an APT repository, activate a slot, or invoke a
package helper manually. Both first install and later upgrade use the stable
entry points:

```sh
curl -fsSL https://install.vonkforge.ai/nas | sh
curl -fsSL https://install.vonkforge.ai/spark | sh
```

Development and stable releases run the same topology and lifecycle. Only the
immutable image, package, setup-program, and manifest identities differ.

## One-time publication authority setup

The workflow separates candidate signing, behavioral canaries, acceptance
signing, and promotion. Create each environment for both `dev` and `stable`:

| Environment | Variables | Secrets |
| --- | --- | --- |
| `installer-candidate-<channel>` | `INSTALLER_PUBLIC_ORIGIN`, `R2_INSTALLER_PUBLIC_BUCKET`, `VONK_INSTALLER_RELEASE_KEY_FINGERPRINT` | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `VONK_INSTALLER_RELEASE_PRIVATE_KEY` |
| `installer-canary-<channel>` | `INSTALLER_PUBLIC_ORIGIN` | `VONK_ACCEPTANCE_LITELLM_UPSTREAM_KEY`, `VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_ID`, `VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_SECRET` |
| `installer-acceptance-<channel>` | `VONK_INSTALLER_ACCEPTANCE_KEY_FINGERPRINT` | `VONK_INSTALLER_ACCEPTANCE_PRIVATE_KEY` |
| `installer-promotion-<channel>` | `R2_INSTALLER_PUBLIC_BUCKET`, `VONK_INSTALLER_ACCEPTANCE_KEY_FINGERPRINT`, `VONK_INSTALLER_RELEASE_KEY_FINGERPRINT` | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `VONK_INSTALLER_RELEASE_PRIVATE_KEY` |

The table is the complete list of environment secrets in each environment. The
canary row's two Tailscale entries are the whole of that environment's tailnet
authority; the `VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_*` names belong to a
child tailnet created at run time and are never stored as environment secrets.

Set `INSTALLER_PUBLIC_ORIGIN=https://install.vonkforge.ai`. Use one dedicated
public R2 bucket and a token restricted to object read/write in only that
bucket. Configure that token independently in candidate and promotion
environments; do not substitute repository-wide credentials or copy NAS
runtime secrets into CI. Prefer workload identity if the selected object-store
client supports it. The R2 S3 publication path requires an access key, so keep
that exception bucket-scoped and rotate it deliberately.

The canary environment holds exactly the two Tailscale secrets named in the
table: `VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_ID` and
`VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_SECRET`. That one factory OAuth
client is the sole owner of the disposable-child-tailnet lifecycle, and it is
the only durable Tailscale credential in the environment or the workflow. It
must carry only the Tailscale `tailnets` write scope, no device tags, and no
other scopes. Never store an operator tailnet's DNS suffix, machine OAuth
credential, or policy in this environment. `VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID`
and `VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET` are not environment
secrets: the create step mints that child-local `auth_keys`-scoped gateway pair
from the child's own response and writes it to `$GITHUB_ENV` for the acceptance
lane. Both the creation path and the cleanup path refuse to touch anything that
is not a `Vonk Forge CI ...` child: the factory is used to create one API-only
child for the native NAS lane and to delete only children that match the CI
display-name pattern, pass id and timestamp validation, and are older than the
stale threshold. The production tailnet is never selected or modified.

The child response contains a new all-scope OAuth credential for that child
only. The workflow masks it immediately, stores it in a mode-`0600` runner file,
never uploads it, and never exports it to the NAS installer. It uses that
lifecycle credential to define `svc:vonk-forge`, `svc:hermes-api`, and
`svc:hermes-dashboard`, each on `tcp:443`, plus only the exact
`tag:vonk-gateway` self-access grant and exact Service auto-approvals required
by the gateway. It then creates and reads back one child-local OAuth client with
only the `auth_keys` scope and only `tag:vonk-gateway`. That scoped client is the
only credential exported to NAS acceptance. Isolation comes from the child
tailnet namespace, not from an `-acceptance` suffix.

Only after the child is configured and the scoped gateway client is verified
does the workflow export its generated DNS suffix, gateway-only OAuth
credential, and
`VONK_ACCEPTANCE_TAILNET_KIND=isolated-disposable-test` to later steps in that
job. The acceptance executable still fails closed when that generated boundary
is absent. A job-level and step-level `always()` finalizer uses the protected
runner state to delete the entire child tailnet after normal completion, step
failure, or a standard cancellation while the runner remains available. Setup
failures after creation also delete the child synchronously. Deletion retries
transient network, rate-limit, conflict, and server failures and treats an exact
child `404` as idempotent success. If retries are exhausted or authentication
fails, the state file is retained and the lane fails rather than reporting
acceptance. Bounded setup and acceptance steps plus explicit job-timeout
headroom reserve time for the finalizer.

The Tailnets API is currently alpha. A hard force-cancel, runner loss, or GitHub
infrastructure termination can prevent every runner-local finalizer from
running, and GitHub Actions cannot make unconditional cleanup claims for those
events. Before creation, the workflow lists API-only tailnets and fails closed
when a `Vonk Forge CI ...` child older than the maximum job lifetime exists. It
reports only the exact child ID and display name and creates nothing new.

Remove that residual with the sanctioned cleanup rather than by hand. Dispatch
`.github/workflows/installer-publication.yml` with the `tailnet_cleanup` input
enabled, optionally passing the exact child id in `child_id`; the job runs
`scripts/tailscale-acceptance-tailnet cleanup` under the `installer-canary-dev`
environment, which is the only place the factory credential exists. Cleanup
deletes only children whose display name matches the CI pattern, whose id and
timestamp validate, and that are older than the stale threshold; it refuses
anything else, caps how many children it will consider and delete, prints each
deleted id and display name, and is safe to run twice because a second run finds
nothing and succeeds. There is no `--force`, and cleanup never widens the
credential at runtime: a factory credential without the `all` scope fails the
token exchange before any deletion. Rerun publication only after the factory
list no longer contains the residual.

The `installer-canary-dev` environment declares exactly two Tailscale secrets --
`VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_ID` and
`VONK_ACCEPTANCE_TAILNET_FACTORY_OAUTH_CLIENT_SECRET` -- and the workflow passes
that pair to the create step and to this cleanup job rather than relying on a
repository-wide secret, because a missing credential must fail at the token
exchange instead of after selecting a child. Those two factory secrets are the
whole of the environment's tailnet authority, the sole owner of the
disposable-child-tailnet lifecycle for both creation and cleanup; the
`VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_*` names exist only as generated
outputs of the child's own response and are never environment secrets. The
factory client holds the `tailnets` write scope alone, so it can create one
child and list the organization's children and cannot delete a child. Cleanup
therefore refuses at the token exchange, and that refusal names the failing
request and the documented `all` requirement instead of guessing at the cause,
because the API's 403 body does not distinguish a scope refusal from any other
403. When it appears, verify in the Tailscale admin console under Trust
credentials which scopes this client id actually holds; an API-only child does
not appear in that console, so this API path is the only cleanup route.

These children belong to the separate Tailscale account that issued the factory
OAuth client, which is why the dispatch above is the primary cleanup interface
rather than a convenience. Running the script on a developer machine cannot work
and is not a defect.

Generate separate RSA-3072 release and acceptance keys on an administrative
workstation. Record each SHA-256 fingerprint from its DER-encoded public key:

```sh
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
  -out vonk-installer-release.pem
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
  -out vonk-installer-acceptance.pem
openssl pkey -in vonk-installer-release.pem -pubout -outform DER \
  | openssl dgst -sha256
openssl pkey -in vonk-installer-acceptance.pem -pubout -outform DER \
  | openssl dgst -sha256
```

Before installing either private key in GitHub, make two human-controlled
recovery copies: one encrypted operator backup, optionally in 1Password, and a
separately encrypted offline escrow. Restore each copy into a temporary
mode-`0600` file, derive its public fingerprint, and require it to match the
recorded fingerprint. Only then install the protected GitHub environment
secrets and remove unencrypted workstation copies. Never rotate or delete an
existing GitHub signing key until the replacement has passed this recovery
test and a complete sign/verify publication exercise.

Map `install.vonkforge.ai` to the bucket as an R2 custom domain. The bucket is
publicly readable but the publication token is write-scoped only to this
bucket. The stable and development channel endpoints embed the same public key;
rotating it is an explicit endpoint rollout, not part of an ordinary release.

The `Installer setup programs` workflow tests and builds the native setup
executables. Publication downloads those exact workflow artifacts by source SHA
and run ID. It never rebuilds them. Release JSON is signed, and one signed,
expiring `current.manifest` atomically advances both `/nas` and `/spark` for a
channel. Stable publication rejects an older semantic version.

The publication workflow refreshes both channel manifests daily. Refresh first
verifies the existing manifest signature, every referenced immutable object and
digest, and the detached release signature; only then does it extend the signed
expiry. A quiet release channel therefore remains installable without
rebuilding or republishing any accepted artifact.
