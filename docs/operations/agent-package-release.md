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

Set `INSTALLER_PUBLIC_ORIGIN=https://install.vonkforge.ai`. Use one dedicated
public R2 bucket and a token restricted to object read/write in only that
bucket. Configure that token independently in candidate and promotion
environments; do not substitute repository-wide credentials or copy NAS
runtime secrets into CI. Prefer workload identity if the selected object-store
client supports it. The R2 S3 publication path requires an access key, so keep
that exception bucket-scoped and rotate it deliberately.

The canary environment must contain a dedicated OAuth factory credential with
only the Tailscale `tailnets` write scope, no device tags, and no other scopes.
Never store an operator tailnet's DNS suffix, machine OAuth credential, or
policy in this environment. The workflow uses the factory solely to create one
API-only child tailnet for the native NAS lane; the production tailnet is never
selected or modified.

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

The Tailnets API is currently alpha. Keep the factory credential scoped only to
tailnet creation and rotate it if Tailscale changes that contract. Never widen
it to `all` merely to recover an interrupted CI run. A hard force-cancel,
runner loss, or GitHub infrastructure termination can prevent every runner-local
finalizer from running; GitHub Actions cannot make unconditional cleanup claims
for those events. Before creation, the workflow lists API-only tailnets and
fails closed when a `Vonk Forge CI ...` child older than the maximum job lifetime
exists. It reports only the exact child ID and display name, creates nothing new,
and never attempts a production-tailnet mutation.

Treat such a residual as a blocked incident. CI must not widen the factory
scope, create credentials in the production tailnet, or mutate any production
resource to recover it. Escalate the exact reported child ID and display name to
Tailscale-supported recovery, or obtain separate explicit authorization for an
administrative recovery procedure outside this CI change. Rerun publication
only after the factory list no longer contains the residual. This is necessary
because an API-only child does not appear in the admin console and its returned
child secret is otherwise available only on the lost runner.

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
