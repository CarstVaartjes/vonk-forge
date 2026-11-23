#![cfg(test)]
use super::*;

pub(crate) fn gid(root: &Path, device: &str, index: &str, address: &str, kind: &str) {
    let port = root.join(device).join("ports/1");
    for dir in ["gids", "gid_attrs/types", "gid_attrs/ndevs"] {
        fs::create_dir_all(port.join(dir)).unwrap();
    }
    fs::write(port.join("state"), "4: ACTIVE\n").unwrap();
    fs::write(port.join("gids").join(index), address).unwrap();
    fs::write(port.join("gid_attrs/types").join(index), kind).unwrap();
    fs::write(port.join("gid_attrs/ndevs").join(index), "enp1s0f1np1\n").unwrap();
}

/// Give an RDMA device its PCI function, as sysfs does with a symlink.
pub(crate) fn pci(root: &Path, device: &str, address: &str, model: &str) {
    let target = root.parent().unwrap().join("pci").join(address);
    fs::create_dir_all(&target).unwrap();
    fs::write(target.join("vendor"), "0x15b3\n").unwrap();
    fs::write(target.join("device"), format!("{model}\n")).unwrap();
    std::os::unix::fs::symlink(&target, root.join(device).join("device")).unwrap();
}

fn twin_ndev(root: &Path, device: &str, index: &str, interface: &str) {
    fs::write(
        root.join(device)
            .join("ports/1/gid_attrs/ndevs")
            .join(index),
        format!("{interface}\n"),
    )
    .unwrap();
}

/// A cabled QSFP port that reaches the host over two `PCIe` links.
fn spark(root: &Path) {
    gid(root, "rocep1s0f1", "3", "::ffff:192.168.100.10", "RoCE v2");
    gid(
        root,
        "roceP2p1s0f1",
        "3",
        "::ffff:192.168.101.10",
        "RoCE v2",
    );
    twin_ndev(root, "roceP2p1s0f1", "3", "enP2p1s0f1np1");
    pci(root, "rocep1s0f1", "0000:01:00.1", "0x1021");
    pci(root, "roceP2p1s0f1", "0002:01:00.1", "0x1021");
}

#[test]
fn both_devices_of_one_cabled_port_are_launched_in_a_fixed_order() {
    let root = tempfile::tempdir().unwrap();
    let sysfs = root.path().join("sysfs");
    spark(&sysfs);
    let address = "192.168.100.10".parse().unwrap();
    let binding = resolve(&sysfs, "enp1s0f1np1", address).unwrap();
    assert_eq!(binding.launch_hca, "=rocep1s0f1:1,roceP2p1s0f1:1");
    assert!(binding.notes.is_empty());
    // The identity names the link's own device only, so a missing or
    // recovered second rail never changes what a run is.
    assert_eq!(binding.identity_hca(), "NCCL_IB_HCA==rocep1s0f1:1");
    assert!(
        binding
            .environment
            .contains(&"NCCL_IB_GID_INDEX=3".to_owned())
    );
    // The same order from the other rank's point of view.
    let other = resolve(&sysfs, "enP2p1s0f1np1", "192.168.101.10".parse().unwrap()).unwrap();
    assert_eq!(other.launch_hca, "=roceP2p1s0f1:1,rocep1s0f1:1");
}

#[test]
fn a_down_second_device_falls_back_to_one_with_a_reason() {
    let root = tempfile::tempdir().unwrap();
    let sysfs = root.path().join("sysfs");
    spark(&sysfs);
    let address = "192.168.100.10".parse().unwrap();
    fs::write(sysfs.join("roceP2p1s0f1/ports/1/state"), "1: DOWN\n").unwrap();
    let binding = resolve(&sysfs, "enp1s0f1np1", address).unwrap();
    assert_eq!(binding.launch_hca, "=rocep1s0f1:1");
    assert_eq!(
        binding.notes,
        ["roceP2p1s0f1 left out: its port is not active"]
    );
    // An unaddressed or differently indexed twin is left out the same way.
    fs::write(sysfs.join("roceP2p1s0f1/ports/1/state"), "4: ACTIVE\n").unwrap();
    fs::write(
        sysfs.join("roceP2p1s0f1/ports/1/gids/3"),
        "0000:0000:0000:0000:0000:0000:0000:0000\n",
    )
    .unwrap();
    let binding = resolve(&sysfs, "enp1s0f1np1", address).unwrap();
    assert_eq!(binding.launch_hca, "=rocep1s0f1:1");
    assert_eq!(binding.notes.len(), 1);
    fs::remove_file(sysfs.join("roceP2p1s0f1/ports/1/gids/3")).unwrap();
    let binding = resolve(&sysfs, "enp1s0f1np1", address).unwrap();
    assert_eq!(binding.launch_hca, "=rocep1s0f1:1");
    assert_eq!(binding.notes.len(), 1);
}

#[test]
fn a_twin_with_a_different_gid_type_or_no_pci_identity_is_not_used() {
    let root = tempfile::tempdir().unwrap();
    let sysfs = root.path().join("sysfs");
    spark(&sysfs);
    let address = "192.168.100.10".parse().unwrap();
    fs::write(
        sysfs.join("roceP2p1s0f1/ports/1/gid_attrs/types/3"),
        "IB/RoCE v1\n",
    )
    .unwrap();
    let binding = resolve(&sysfs, "enp1s0f1np1", address).unwrap();
    assert_eq!(binding.launch_hca, "=rocep1s0f1:1");
    assert_eq!(binding.notes.len(), 1);
    fs::remove_file(sysfs.join("rocep1s0f1/device")).unwrap();
    let binding = resolve(&sysfs, "enp1s0f1np1", address).unwrap();
    assert_eq!(binding.launch_hca, "=rocep1s0f1:1");
}

#[test]
fn devices_on_other_ports_or_models_are_never_mixed_in() {
    let root = tempfile::tempdir().unwrap();
    let sysfs = root.path().join("sysfs");
    spark(&sysfs);
    // The NIC's other QSFP port: another PCI function, up and addressed.
    gid(
        &sysfs,
        "rocep1s0f0",
        "3",
        "::ffff:192.168.102.10",
        "RoCE v2",
    );
    pci(&sysfs, "rocep1s0f0", "0000:01:00.0", "0x1021");
    gid(
        &sysfs,
        "roceP2p1s0f0",
        "3",
        "::ffff:192.168.103.10",
        "RoCE v2",
    );
    pci(&sysfs, "roceP2p1s0f0", "0002:01:00.0", "0x1021");
    // The same function in a third domain, but another kind of device.
    gid(
        &sysfs,
        "mlx5_other",
        "3",
        "::ffff:192.168.104.10",
        "RoCE v2",
    );
    pci(&sysfs, "mlx5_other", "0003:01:00.1", "0x1017");
    let address = "192.168.100.10".parse().unwrap();
    let binding = resolve(&sysfs, "enp1s0f1np1", address).unwrap();
    assert_eq!(binding.launch_hca, "=rocep1s0f1:1,roceP2p1s0f1:1");
    assert!(binding.notes.is_empty());
}

#[test]
fn a_device_without_a_pci_function_gets_one_rail_and_no_note() {
    let root = tempfile::tempdir().unwrap();
    let sysfs = root.path().join("sysfs");
    gid(&sysfs, "rxe0", "3", "::ffff:192.168.100.10", "RoCE v2");
    let binding = resolve(&sysfs, "enp1s0f1np1", "192.168.100.10".parse().unwrap()).unwrap();
    assert_eq!(binding.launch_hca, "=rxe0:1");
    assert!(binding.notes.is_empty());
}

#[test]
fn only_the_exact_active_roce_v2_address_authorizes_binding() {
    let root = tempfile::tempdir().unwrap();
    gid(
        root.path(),
        "rocep1s0f1",
        "2",
        "::ffff:192.168.100.10",
        "IB/RoCE v1",
    );
    gid(
        root.path(),
        "rocep1s0f1",
        "3",
        "::ffff:192.168.100.10",
        "RoCE v2",
    );
    let address = "192.168.100.10".parse().unwrap();
    let env = resolve(root.path(), "enp1s0f1np1", address)
        .unwrap()
        .environment;
    assert!(env.contains(&"NCCL_IB_HCA==rocep1s0f1:1".to_owned()));
    assert!(env.contains(&"NCCL_IB_GID_INDEX=3".to_owned()));
    assert!(resolve(root.path(), "wlP9s9", address).is_err());
    assert!(
        resolve(
            root.path(),
            "enp1s0f1np1",
            "192.168.100.11".parse().unwrap()
        )
        .is_err()
    );
    gid(
        root.path(),
        "another-device",
        "3",
        "::ffff:192.168.100.10",
        "RoCE v2",
    );
    assert!(resolve(root.path(), "enp1s0f1np1", address).is_err());
}
