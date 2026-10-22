//! Store ownership shared by the unprivileged agent and privileged helper.
use Collector::*;
use StoreRoot::*;

/// All unused Spark data, including model copies, expires after one day.
pub const GRACE: std::time::Duration = std::time::Duration::from_secs(24 * 60 * 60);

/// How one store's entries leave.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Collector {
    /// Only on a Controller order (uninstall, job scope cleanup).
    ControllerOrder,
    /// The claim that made it removes it before the claim ends.
    Claim,
    /// One bounded file or credential set the agent rewrites in place.
    Bookkeeping,
    /// Unused entries, once unchanged for [`GRACE`].
    Ended,
    /// Unused model copies leave after [`GRACE`] or on low space; scratch is [`Ended`].
    ModelObjects,
    /// A base image no build has opened for [`GRACE`], and isolated entries.
    BaseImages,
    /// Private build scratch, after its systemd units have settled for collection.
    BuildStaging,
    /// An installation the Controller has not tracked for [`GRACE`] and no
    /// started run names, and isolated entries; one it tracks only on its order.
    Untracked,
    /// A run scope no run or job of the Controller's has owned for [`GRACE`],
    /// and isolated entries; one it tracks only on its order.
    UntrackedRuns,
    /// Cleanup history nothing reads again and the checkpoint of an
    /// installation that is gone, once unchanged for [`GRACE`]; a quarantined
    /// installation (`<id>.partial`) only on the Controller's order.
    Reconciliation,
    /// The lock file of an installation that is gone, once unchanged for
    /// [`GRACE`] and not held.
    Ownership,
}

/// A name below its process's data root (directories end with `/`), its
/// collector and owner. A single `*` names disposable top-level scratch.
#[derive(Debug)]
pub struct Store(
    pub &'static str,
    pub Collector,
    pub &'static str,
    pub StoreRoot,
);

/// The process and privilege boundary that owns a store.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum StoreRoot {
    Agent,
    Helper,
}

impl StoreRoot {
    /// Resolve a dynamic store name only through the ownership registry.
    pub fn path(self, root: &std::path::Path, name: &str) -> Option<std::path::PathBuf> {
        STORES
            .iter()
            .find(|store| store.3 == self && store.0.trim_end_matches('/') == name)
            .map(|store| store.path(root))
    }
}

impl Store {
    pub fn path(&self, root: &std::path::Path) -> std::path::PathBuf {
        root.join(self.0.trim_end_matches('/'))
    }
}

/// Every top-level name the agent, its helper or its package writes below the
/// data root. A new store cannot be added without an owner and a collector.
#[rustfmt::skip]
pub const STORES: &[Store] = &[
    // First: a removed installation's model objects are unused in the same pass.
    Store("installations/", Untracked, "the Controller's installation record; its verdict", Agent),
    Store("distribution/", ModelObjects, "installations linking or naming it; its transfer", Agent),
    Store("installation-reconciliation/", Reconciliation, "the Controller's uninstall", Agent),
    Store("installation-ownership/", Ownership, "one lock file per installation", Agent),
    Store("runs/", UntrackedRuns, "the Controller's run or job record; its verdict", Agent),
    Store("run-metadata/", UntrackedRuns, "the Controller's run or job record; its verdict", Agent),
    Store("builds/", Ended, "the build claim, until the Controller has the image", Agent),
    Store("build-staging/", BuildStaging, "the build claim and its exact systemd units", Agent),
    Store("base-images/", BaseImages, "the builds that open each image", Agent),
    Store("containers/", Claim, "Podman's default storage; builds use private storage", Agent),
    Store("tmp/", Claim, "the process that made each temporary file", Agent),
    Store("credentials/", Bookkeeping, "the agent identity", Agent),
    Store("state.sqlite", Bookkeeping, "the agent journal", Agent),
    Store("store-retention.json", Bookkeeping, "the Controller inventory verdict", Agent),
    Store("installation-inventory-cursor", Bookkeeping, "inventory page rotation", Agent),
    Store("run-inventory-cursor", Bookkeeping, "inventory page rotation", Agent),
    Store(".state-db-reserve", Bookkeeping, "the agent journal", Agent),
    Store("preflight-completion/", Bookkeeping, "one bounded fence-bound completed preflight receipt", Agent),
    Store("runtime-preflight-cache.json", Bookkeeping, "the host fingerprint", Agent),
    Store("registry-auth.json", Bookkeeping, "the base-image registry credential", Agent),
    Store(".config/", Bookkeeping, "the rootless Podman configuration", Agent),
    Store(".machine-evidence.*", Claim, "the package step publishing machine-evidence; its cleanup trap", Agent),
    Store("machine-evidence", Bookkeeping, "the package", Agent),
    Store("gpu-clock-observation.json", Bookkeeping, "the helper's latest GPU clock observation", Agent),
    // Interrupted quarantine renames are unused scratch, including at the root.
    Store(".preflight-*", Ended, "damaged generated preflight state", Agent),
    Store(".build-staging-*", Ended, "isolated build staging", Agent),
    Store("package-rollback/", Bookkeeping, "one current rollback transaction, source package and binaries", Helper),
    Store("package-upgrade/", Bookkeeping, "the current signed package recovery intent", Helper),
    Store(".helper-upgrade.*", Ended, "an interrupted package activation fence publication", Helper),
    Store("helper-upgrade.receipt", Bookkeeping, "the latest bounded helper activation receipt", Helper),
    Store("helper-upgrade.pending", Bookkeeping, "the current package activation fence", Helper),
    Store("package-activation.receipt.json", Bookkeeping, "the latest bounded package activation receipt", Helper),
    Store(".activation-*.json", Ended, "an interrupted activation receipt publication", Helper),
    Store(".retired-*", Ended, "isolated package rollback state", Helper),
    Store("gpu-clock/", Bookkeeping, "one current checkpoint and lock; interrupted writes expire", Helper),
    Store("gpu-clock-observation.json", Bookkeeping, "the helper's latest GPU clock observation", Helper),
    Store(".gpu-clock-*.tmp", Ended, "an interrupted GPU clock observation publication", Helper),
    Store("runtime-identities/", Ended, "UID/GID identity files while mounted by a container", Helper),
    Store("installation-intent-fences/", Reconciliation, "the installation's current nonce and ordinal", Helper),
    Store("runtime-generation-fences/", Reconciliation, "a runtime's generation; a miss challenges current intent", Helper),
    Store("installation-reconciliation/", Reconciliation, "the helper's installation cleanup checkpoint", Helper),
    Store("runtime-ownership/", Ownership, "one runtime lock per installation", Helper),
    Store("job-exits/", Ended, "a completed job's replayable exit for one day", Helper),
    Store("host-memory-evidence/", Claim, "the host memory guard; absent containers' receipts", Helper),
    Store("compile-cache/", Claim, "content identities; mounted containers pin entries, otherwise one day or disk pressure", Helper),
    Store("compile-cache-fallback/", Claim, "fresh per-run compile scratch; the same mount pins, grace and pressure collector", Helper),
    Store("runtime-images/", Claim, "the existing unused runtime-image collector", Helper),
    Store("incoming/", Ended, "a completed or interrupted package transfer", Helper),
    Store("runtime-requests/", Ended, "a signed request whose five-minute grant expired", Helper),
    Store("helper/requests/", Ended, "consumed five-minute grants; one-day replay protection", Helper),
    Store("helper/package-candidates/", Claim, "the package custody collector", Helper),
    // Retired: nothing writes or reads these any more.
    Store("models/", Ended, "none: the model store before installations", Agent),
    Store("oci-archives/", Ended, "none: image archives before the layered store", Agent),
    Store("image-imports/", Ended, "none: Docker imports before the layered store", Agent),
];
