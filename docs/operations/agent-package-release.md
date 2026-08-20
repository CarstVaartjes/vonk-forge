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
