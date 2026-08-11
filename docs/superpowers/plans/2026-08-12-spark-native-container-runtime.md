# Spark-native container runtime implementation plan

1. Add cross-language protocol tests for the typed runtime request and the
   signed host-helper operation that binds its action and digest.
2. Extend the controller authority route to issue grants only for the exact
   active, certificate-bound operation/action pair.
3. Extend the Rust helper with stable request-file verification, compiled
   Docker image import/inspection/run/remove operations, bounded results, and
   replay-safe execution.
4. Add the Rust agent helper client and refactor image import/install/start/stop
   to request grants and submit canonical runtime requests. Keep rootless
   Podman only in the recipe builder.
5. Update inventory capabilities, package dependencies, systemd boundaries,
   package verification, and installation preflight for DGX Spark Docker plus
   NVIDIA Container Toolkit.
6. Update fresh-install, agent, platform-update, fabric, architecture, and
   acceptance documentation with the supported Spark contract.
7. Run focused tests, full Rust/control/package suites, package lifecycle, and
   security verification; request review and merge.
8. Publish through GitHub Actions, canary Spark 1, complete synthetic and real
   slices, then update Spark 2 and clean temporary access.
