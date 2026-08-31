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
| `installer-canary-<channel>` | `INSTALLER_PUBLIC_ORIGIN`, `VONK_ACCEPTANCE_TAILNET_DNS_SUFFIX`, `VONK_ACCEPTANCE_TAILNET_KIND=isolated-disposable-test` | `VONK_ACCEPTANCE_LITELLM_UPSTREAM_KEY`, `VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_ID`, `VONK_ACCEPTANCE_TAILSCALE_OAUTH_CLIENT_SECRET` |
| `installer-acceptance-<channel>` | `VONK_INSTALLER_ACCEPTANCE_KEY_FINGERPRINT` | `VONK_INSTALLER_ACCEPTANCE_PRIVATE_KEY` |
| `installer-promotion-<channel>` | `R2_INSTALLER_PUBLIC_BUCKET`, `VONK_INSTALLER_ACCEPTANCE_KEY_FINGERPRINT`, `VONK_INSTALLER_RELEASE_KEY_FINGERPRINT` | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `VONK_INSTALLER_RELEASE_PRIVATE_KEY` |

Set `INSTALLER_PUBLIC_ORIGIN=https://install.vonkforge.ai`. Use one dedicated
public R2 bucket and a token restricted to object read/write in only that
bucket. Configure that token independently in candidate and promotion
environments; do not substitute repository-wide credentials or copy NAS
runtime secrets into CI. Prefer workload identity if the selected object-store
client supports it. The R2 S3 publication path requires an access key, so keep
that exception bucket-scoped and rotate it deliberately.

The canary environment must point to a dedicated disposable test tailnet, never
an operator tailnet. `VONK_ACCEPTANCE_TAILNET_KIND` is a fail-closed attestation;
the executable rejects full Tailscale acceptance when it is absent or has any
other value. Use separate disposable OAuth credentials with only `auth_keys`
write scope and only `tag:vonk-gateway`. The test tailnet separately owns its
Service definitions, exact self-access grants, tests, and Service-host
auto-approval. After each acceptance campaign, confirm no CI gateway remains;
when retiring the tailnet, remove its nodes, Service definitions, grants/tests,
auto-approvers, OAuth client, and external provider credentials. Never copy any
of those resources into an operator tailnet.

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
