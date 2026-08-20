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

The repository needs the protected GitHub environments `installer-dev` and
`installer-stable`. Configure the same values in both environments:

- variable `INSTALLER_PUBLIC_ORIGIN=https://install.vonkforge.ai`;
- variable `R2_INSTALLER_PUBLIC_BUCKET` containing the dedicated public R2
  bucket name;
- secrets `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY` for a
  token restricted to object read/write in that bucket;
- secret `VONK_INSTALLER_RELEASE_PRIVATE_KEY` containing one RSA-3072 PEM key;
  and
- variable `VONK_INSTALLER_RELEASE_KEY_FINGERPRINT` containing the SHA-256 of
  that key's DER-encoded public key.

Generate the installer key once on an administrative workstation, record the
public-key fingerprint, install the private PEM as the protected secret in both
environments, and then remove the workstation copy:

```sh
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 \
  -out vonk-installer-release.pem
openssl pkey -in vonk-installer-release.pem -pubout -outform DER \
  | openssl dgst -sha256
```

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
