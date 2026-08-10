# GPU node agent package channels

Vonk Forge publishes one reproducible ARM64 Debian package through two explicit
channels. An accepted exact `main` tip published by the dedicated development
GitHub Actions workflow produces
`X.Y.Z~dev.<workflow-run-number>+g<sha12>` and advances apt distribution `dev`.
The workflow supplies its monotonic `github.run_number` as the official
publication sequence; reruns of the same workflow run retain that number.
Local tooling may validate an explicitly supplied fixture sequence, but cannot
select or publish an official sequence. A trusted annotated, SSH-signed
`vX.Y.Z` tag reachable from `main` produces `X.Y.Z`, attaches the accepted
package evidence to that tag's GitHub Release, and then advances apt
distribution `stable`.

There is no mutable `.deb` called `dev` or `latest`. Development Actions
artifacts are named with the full source commit; production files are immutable
GitHub Release assets. The apt distributions are moving discovery channels.

## Trust and secret boundaries

Three independent authorities are intentional:

- The agent Ed25519 key signs the packaged A/B slot and host-helper
  authorization. Its public-key digest is verified before every build.
- The development and production apt OpenPGP keys independently sign their
  channel's `InRelease` metadata. Apt authenticates the package before the
  embedded agent key is trusted.
- GitHub OIDC creates short-lived keyless Sigstore bundles and GitHub artifact
  attestations. No stored OIDC credential exists.

The package build never receives apt or R2 credentials. The apt publisher never
receives `VONK_AGENT_RELEASE_PRIVATE_KEY`. Development and production use
different apt signing keys and different private state buckets. Never copy an
apt key or private state bucket between `apt-development` and `apt-release`.
The two agent environments deliberately contain the same `VONK_AGENT_RELEASE_PRIVATE_KEY`
and matching fingerprint so all accepted
packages retain one embedded agent trust root.

Runtime secrets and NAS secrets—including database passwords, database URLs,
Git signing keys, generated API/worker authority, controller tokens, mTLS
identities, Tailscale credentials, and model-provider keys—never enter a
package, container image, Git tree, GitHub Actions artifact, CI log,
attestation, SBOM, provenance document, or Compose artifact. They are created
and mounted as runtime files on the target host; see the
[NAS runtime-secret guide](../runbooks/development-nas-installation.md).
These runtime secrets remain outside every release supply-chain boundary.

## Configure the four GitHub environments

Create all four environments before enabling publication. Protect them with
deployment branch/tag rules and required reviewers appropriate to their
authority. Repository-level secrets are not a substitute for environment
secrets.

### `agent-development`

Allow only `main`. Configure:

- secret `VONK_AGENT_RELEASE_PRIVATE_KEY`: the unencrypted PEM Ed25519 private
  key used by the package builder;
- variable `VONK_AGENT_RELEASE_KEY_FINGERPRINT`: lowercase SHA-256 of the raw
  32-byte Ed25519 public key; and
- no apt, R2, registry, production, controller, or runtime credential.

### `agent-release`

Allow only protected production tags. Configure the same
`VONK_AGENT_RELEASE_PRIVATE_KEY` secret and the same
`VONK_AGENT_RELEASE_KEY_FINGERPRINT` variable as `agent-development`. Configure
no apt, R2, controller, NAS, or runtime credential.

Create and fingerprint the shared agent key on an offline administrator host:

```bash
umask 077
openssl genpkey -algorithm ED25519 -out vonk-agent-release.pem
openssl pkey -in vonk-agent-release.pem -pubout -outform DER \
  > vonk-agent-release-public.der
test "$(stat -c %s vonk-agent-release-public.der)" = 44
test "$(head -c 12 vonk-agent-release-public.der | od -An -v -tx1 | tr -d ' \n')" \
  = 302a300506032b6570032100
tail -c 32 vonk-agent-release-public.der | sha256sum
```

Store the displayed 64-character lowercase digest as the fingerprint variable.
Keep encrypted offline backups and a written revocation procedure. The workflow
rejects another algorithm, malformed PEM, or a mismatched fingerprint before
building.

### `apt-development`

Allow only `main`. Configure:

- secret `APT_REPOSITORY_GPG_PRIVATE_KEY`: exported private OpenPGP key used
  only for the `dev` repository;
- secret `APT_GPG_PASSPHRASE`: that key's passphrase;
- secrets `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY`: a
  Cloudflare R2 token limited to the public apt bucket and the development apt
  state bucket;
- variable `APT_REPOSITORY_GPG_FINGERPRINT`: the full 40-character uppercase
  primary-key fingerprint;
- variable `R2_APT_PUBLIC_BUCKET`: the bucket served as
  `https://packages.vonkforge.ai`; and
- variable `R2_APT_STATE_BUCKET`: a private development-only bucket with no
  public endpoint.

### `apt-release`

Allow only protected production tags and require a production reviewer.
Configure the same secret and variable names as `apt-development`, but use a
different OpenPGP private key, passphrase, R2 access token, and private stable
state bucket. `R2_APT_PUBLIC_BUCKET` may name the same public bucket because the
publisher owns disjoint `dists/dev` and `dists/stable` trees and fixed keyring
filenames. The production token should be limited to that public bucket and
the production state bucket only.

For each apt environment, generate a dedicated expiring certification/signing
key offline, export its encrypted private key for the environment secret, and
record the complete primary fingerprint through an independent channel. Verify
the exported key before upload:

```bash
gpg --batch --with-colons --show-keys apt-private-key.asc \
  | awk -F: '$1 == "fpr" { print $10; exit }'
```

The workflow accepts exactly one secret primary key and requires the result to
equal `APT_REPOSITORY_GPG_FINGERPRINT`.

## R2 layout and publication safety

Bind only the public bucket to `packages.vonkforge.ai`. Both state buckets stay
private and must have object versioning/backups appropriate to signing state.
The public roots include:

```text
dists/dev/InRelease
dists/stable/InRelease
pool/...
vonk-forge-dev-archive-keyring.gpg
vonk-forge-archive-keyring.gpg
```

Each private channel records immutable
`versions/<version>/aptly-state.tar.gz`,
`versions/<version>/public-tree.tar.gz`, and
`versions/<version>/commit.json`; `latest.json` is only a pointer. The commit
manifest binds both archives by size and SHA-256 and is written before public
publication. Package/index data is uploaded before `Release` and
`Release.gpg`; `InRelease` is the final public commit object; `latest.json`
advances last. Cache `pool/` objects for a long period, but require short TTL or
revalidation for `InRelease`, `Release`, and `Packages*`.

## Install the `dev` channel

Obtain the development apt fingerprint from an independently authenticated
operator record, not from the same bucket as the key. Set it explicitly and
require an exact match before installing the keyring:

```bash
(
set -euo pipefail
EXPECTED_DEV_APT_FINGERPRINT='REPLACE_WITH_40_CHARACTER_UPPERCASE_FINGERPRINT'
curl --fail --proto '=https' --tlsv1.3 \
  --output /tmp/vonk-forge-dev-archive-keyring.gpg \
  https://packages.vonkforge.ai/vonk-forge-dev-archive-keyring.gpg
verify_single_primary_keyring() {
  local keyring=$1 expected=$2 metadata
  [[ "$expected" =~ ^[0-9A-F]{40}$ ]]
  metadata=$(gpg --batch --show-keys --with-colons "$keyring")
  LC_ALL=C awk -F: -v expected="$expected" '
    $1 == "pub" { primaries++; awaiting_fingerprint=1; next }
    awaiting_fingerprint {
      if ($1 != "fpr" || length($10) != 40 || $10 ~ /[^0-9A-F]/) {
        malformed=1
      } else {
        fingerprints++
        observed=$10
      }
      awaiting_fingerprint=0
    }
    END {
      if (primaries != 1 || fingerprints != 1 || malformed || observed != expected) {
        exit 1
      }
    }
  ' <<<"$metadata"
}
verify_single_primary_keyring \
  /tmp/vonk-forge-dev-archive-keyring.gpg \
  "$EXPECTED_DEV_APT_FINGERPRINT"
sudo install -o root -g root -m 0644 \
  /tmp/vonk-forge-dev-archive-keyring.gpg \
  /usr/share/keyrings/vonk-forge-dev-archive-keyring.gpg
printf '%s\n' \
  'deb [arch=arm64 signed-by=/usr/share/keyrings/vonk-forge-dev-archive-keyring.gpg] https://packages.vonkforge.ai dev main' \
  | sudo tee /etc/apt/sources.list.d/vonk-forge-dev.list >/dev/null
sudo apt update
sudo apt install vonk-forge-agent
)
```

## Install the `stable` channel

Use the independently recorded production fingerprint:

```bash
(
set -euo pipefail
EXPECTED_STABLE_APT_FINGERPRINT='REPLACE_WITH_40_CHARACTER_UPPERCASE_FINGERPRINT'
curl --fail --proto '=https' --tlsv1.3 \
  --output /tmp/vonk-forge-archive-keyring.gpg \
  https://packages.vonkforge.ai/vonk-forge-archive-keyring.gpg
verify_single_primary_keyring() {
  local keyring=$1 expected=$2 metadata
  [[ "$expected" =~ ^[0-9A-F]{40}$ ]]
  metadata=$(gpg --batch --show-keys --with-colons "$keyring")
  LC_ALL=C awk -F: -v expected="$expected" '
    $1 == "pub" { primaries++; awaiting_fingerprint=1; next }
    awaiting_fingerprint {
      if ($1 != "fpr" || length($10) != 40 || $10 ~ /[^0-9A-F]/) {
        malformed=1
      } else {
        fingerprints++
        observed=$10
      }
      awaiting_fingerprint=0
    }
    END {
      if (primaries != 1 || fingerprints != 1 || malformed || observed != expected) {
        exit 1
      }
    }
  ' <<<"$metadata"
}
verify_single_primary_keyring \
  /tmp/vonk-forge-archive-keyring.gpg \
  "$EXPECTED_STABLE_APT_FINGERPRINT"
sudo install -o root -g root -m 0644 \
  /tmp/vonk-forge-archive-keyring.gpg \
  /usr/share/keyrings/vonk-forge-archive-keyring.gpg
printf '%s\n' \
  'deb [arch=arm64 signed-by=/usr/share/keyrings/vonk-forge-archive-keyring.gpg] https://packages.vonkforge.ai stable main' \
  | sudo tee /etc/apt/sources.list.d/vonk-forge.list >/dev/null
sudo apt update
sudo apt install vonk-forge-agent
)
```

Keep only one Vonk Forge source enabled during normal operation. Installation
is offline-safe after apt has cached the `.deb`; maintainer scripts create only
local users, directories, A/B state, and systemd enablement. Pairing is a
separate explicit controller operation.

## Update and switch channels

Inspect the installed and candidate versions before every channel change:

```bash
dpkg-query -W -f='${Version}\n' vonk-forge-agent
apt-cache policy vonk-forge-agent
sudo apt update
sudo apt install --only-upgrade vonk-forge-agent
```

Debian orders `X.Y.Z~dev.<workflow-run-number>+g<sha12>` before `X.Y.Z`, so
moving from a development build for a release line to that final release is an
upgrade. Newer development workflow run numbers sort after older workflow run
numbers. Confirm a proposed ordering when needed:

```bash
dpkg --compare-versions "$CANDIDATE_VERSION" gt "$INSTALLED_VERSION"
```

To move from `dev` to `stable`, first install and verify the stable keyring,
write the stable source, remove the development source, then update:

```bash
sudo rm -f /etc/apt/sources.list.d/vonk-forge-dev.list
sudo apt update
apt-cache policy vonk-forge-agent
sudo apt install vonk-forge-agent
```

Reverse the source filenames to move to `dev`. Merely changing the source does not bypass
Debian or package downgrade protection. If the selected channel's
candidate is older than the installed version, apt will not downgrade it and
the package `preinst` also refuses a downgrade. Wait for that channel to catch
up or use the separately reviewed recovery procedure; do not add
`--allow-downgrades` and do not remove the package just to evade this guard.

## Verify immutable development evidence

Select the successful `Agent package release` run for the accepted `main` SHA,
then download the full-SHA artifact:

```bash
SOURCE_SHA='REPLACE_WITH_40_CHARACTER_MAIN_SHA'
gh run list --workflow agent-release.yml --branch main --commit "$SOURCE_SHA"
gh run download RUN_ID --name "vonk-agent-development-$SOURCE_SHA" --dir agent-package
cd agent-package
sha256sum --check vonk-forge-agent_*_arm64.deb.sha256
gh attestation verify vonk-forge-agent_*_arm64.deb \
  --repo CarstVaartjes/vonk-forge \
  --signer-workflow CarstVaartjes/vonk-forge/.github/workflows/agent-package-build.yml \
  --source-digest "$SOURCE_SHA" --source-ref refs/heads/main
cosign verify-blob \
  --bundle vonk-forge-agent_*_arm64.deb.sigstore.json \
  --certificate-identity-regexp '^https://github.com/CarstVaartjes/vonk-forge/.github/workflows/agent-package-build.yml@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  vonk-forge-agent_*_arm64.deb
```

Use an exact filename if more than one package is present; shell globs must
resolve to one file.

## Verify immutable production evidence

Download the exact signed tag's Release assets and verify checksum, GitHub
attestation, and Sigstore identity before a manual install:

```bash
TAG=v0.1.0
VERSION=${TAG#v}
gh release download "$TAG" --repo CarstVaartjes/vonk-forge \
  --dir "release-$VERSION"
cd "release-$VERSION"
sha256sum --check "vonk-forge-agent_${VERSION}_arm64.deb.sha256"
gh attestation verify "vonk-forge-agent_${VERSION}_arm64.deb" \
  --repo CarstVaartjes/vonk-forge \
  --signer-workflow CarstVaartjes/vonk-forge/.github/workflows/agent-package-build.yml \
  --source-ref "refs/tags/$TAG"
cosign verify-blob \
  --bundle "vonk-forge-agent_${VERSION}_arm64.deb.sigstore.json" \
  --certificate-identity-regexp "^https://github.com/CarstVaartjes/vonk-forge/.github/workflows/agent-package-build\\.yml@refs/tags/${TAG}$" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "vonk-forge-agent_${VERSION}_arm64.deb"
```

The Release also contains the CycloneDX/SPDX SBOMs, SLSA provenance,
`vonk-forge-systemd-security.json`, and the Sigstore bundle from the accepted
reusable build. GitHub attestations remain in GitHub's attestation store and
are verified with `gh attestation verify`; they are not fabricated as a local
JSON file.

## Publication procedure

For development, merge to `main`. The workflow verifies the exact current
`origin/main` tip before build and again inside the protected
`apt-development` job immediately before private-state access. A superseded
commit cannot advance `dev`.

For production:

1. Run the complete read-only acceptance on the intended `main` commit.
2. Confirm all workspace versions equal `X.Y.Z` and the canonical platform
   input exists.
3. Create and push one annotated trusted SSH-signed `vX.Y.Z` tag. Never move or
   reuse it.
4. Approve `agent-release`; verify the package and platform evidence.
5. Confirm the GitHub Release exists with the exact accepted package set.
6. Approve `apt-release`. Stable apt publication cannot start before successful
   GitHub Release creation.
7. Verify `InRelease` and install from a disposable Ubuntu 24.04 ARM64 host.

## Interrupted publication and recovery

An interrupted run is normally recovered by rerunning the exact accepted
`main` commit or signed tag. Before rerunning, preserve logs and inspect the
channel's private bucket:

- no `commit.json`: the attempt is uncommitted; exact matching partial archives
  may be completed, while conflicting bytes fail closed;
- `versions/<version>/commit.json` exists: it is authoritative even when
  `latest.json` is absent or stale; an exact rerun replays the checksum-bound
  public tree and then repairs the pointer;
- publication stopped before `InRelease`: clients continue to see the prior
  signed commit object; and
- publication wrote `InRelease` but not `latest.json`: clients can use the new
  repository while an exact rerun safely repairs private discovery state.

Do not reconstruct trusted aptly state from the public bucket. Do not edit
`latest.json`, delete immutable version objects, or copy state between channels. Restore a
private bucket only from its authenticated, same-channel backup, then rerun the
exact source. If any committed hash differs, signing authority is uncertain,
or a supposedly immutable version conflicts, stop publication and begin an
incident review.

## Key rotation

Do not rotate either authority by silently replacing one environment secret.

For an apt key rotation, first implement and review an overlap release that
publishes a separately named new keyring while metadata is still signed by the
old key. Publish both fingerprints out of band, migrate clients so `signed-by`
trusts the reviewed overlap, and only then switch the environment signer in a
new version. Retain old signed metadata and an offline revocation certificate
through the migration window. The current fixed keyring workflow intentionally
fails closed rather than pretending a one-step signer replacement is safe.

For an agent key rotation, use the still-trusted apt/Sigstore identities to
ship a specifically reviewed supervisor/helper transition package. Update the
same key and fingerprint in both agent environments, canary the transition,
and only then sign later slots with the new key. A suspected compromise freezes
both development and production publication until incident review determines
which trust root remains valid.
