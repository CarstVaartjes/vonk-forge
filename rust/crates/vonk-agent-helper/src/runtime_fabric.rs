//! Bind native collective traffic to the configured address's exact RoCE v2 GID.
//! These are observations of kernel-owned sysfs; no host configuration is changed.
use std::{
    fs, io,
    net::{Ipv4Addr, Ipv6Addr},
    path::Path,
};

#[derive(Debug, thiserror::Error)]
pub enum FabricError {
    #[error("native fabric observation failed")]
    Io(#[from] io::Error),
    #[error("native fabric has no unique active RoCE v2 binding")]
    Unavailable,
}

pub const ENVIRONMENT_NAMES: [&str; 5] = [
    "NCCL_SOCKET_IFNAME",
    "NCCL_IB_HCA",
    "NCCL_IB_GID_INDEX",
    "TP_SOCKET_IFNAME",
    "GLOO_SOCKET_IFNAME",
];

pub fn valid_interface(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 15
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
}

fn read(path: &Path) -> Result<String, FabricError> {
    use std::io::Read;
    let mut value = String::new();
    fs::File::open(path)?.take(257).read_to_string(&mut value)?;
    if value.len() > 256 {
        return Err(FabricError::Unavailable);
    }
    Ok(value.trim().to_owned())
}

fn entries(path: &Path, limit: usize) -> Result<Vec<fs::DirEntry>, FabricError> {
    let mut entries = Vec::new();
    for entry in fs::read_dir(path)? {
        if entries.len() == limit {
            return Err(FabricError::Unavailable);
        }
        entries.push(entry?);
    }
    Ok(entries)
}

/// Reconstruct disposable firewall interface output from the kernel's exact
/// signed local address. Discovery cannot change firewall rules or addresses.
pub fn resolve_address(root: &Path, address: Ipv4Addr) -> Result<Binding, FabricError> {
    let mut interfaces = std::collections::BTreeSet::new();
    for device in entries(root, 32)? {
        for port in entries(&device.path().join("ports"), 8)? {
            for gid in entries(&port.path().join("gids"), 256)? {
                let observed = read(&gid.path())
                    .ok()
                    .and_then(|value| value.parse::<Ipv6Addr>().ok());
                if observed != Some(address.to_ipv6_mapped()) {
                    continue;
                }
                let interface = read(&port.path().join("gid_attrs/ndevs").join(gid.file_name()))?;
                if valid_interface(&interface) {
                    interfaces.insert(interface);
                }
            }
        }
    }
    let mut bindings = interfaces
        .into_iter()
        .filter_map(|interface| resolve(root, &interface, address).ok());
    let binding = bindings.next().ok_or(FabricError::Unavailable)?;
    if bindings.next().is_some() {
        return Err(FabricError::Unavailable);
    }
    Ok(binding)
}

/// NCCL merges at most this many devices into one virtual NIC and fails the
/// whole bind above it.
const MAX_RAILS: usize = 4;

/// The platform-owned binding for one distributed rank.
#[derive(Debug, PartialEq, Eq)]
pub struct Binding {
    /// Environment naming only the device whose GID matches the link address.
    /// This is what the container identity is computed over: whether a second
    /// rail happens to be up must never decide whether an existing run is the
    /// run that was asked for.
    pub environment: Vec<String>,
    /// `NCCL_IB_HCA` as launched: the link's own device first, then every other
    /// up, addressed device of the same cabled port in a fixed order. Equal to
    /// the identity value when only one device qualifies.
    pub launch_hca: String,
    /// Why a same-port device was left out. Observations, never failures.
    pub notes: Vec<String>,
}

impl Binding {
    pub fn rails(&self) -> usize {
        self.launch_hca.matches(',').count() + 1
    }

    pub fn identity_hca(&self) -> &str {
        self.environment
            .iter()
            .find(|value| value.starts_with("NCCL_IB_HCA="))
            .map_or("", String::as_str)
    }
}

struct Pci {
    domain: String,
    function: String,
    vendor: String,
    model: String,
}

/// The PCI function behind an RDMA device. A DGX Spark reaches one cabled QSFP
/// port over two PCIe links: the same bus/slot/function in two PCI domains.
fn pci(device: &Path) -> Option<Pci> {
    let target = fs::read_link(device.join("device")).ok()?;
    let address = target.file_name()?.to_str()?;
    let (domain, function) = address.split_once(':')?;
    Some(Pci {
        domain: domain.to_owned(),
        function: function.to_owned(),
        vendor: read(&device.join("device/vendor")).ok()?,
        model: read(&device.join("device/device")).ok()?,
    })
}

/// Other RDMA devices on the same cabled port as `primary`, in a fixed order,
/// that can carry the link at the same GID index (NCCL takes one index for all
/// devices), and why any same-port device could not.
fn same_port_rails(
    root: &Path,
    devices: &[String],
    primary: &str,
    port: &str,
    index: &str,
) -> (Vec<String>, Vec<String>) {
    let mut rails = Vec::new();
    let mut notes = Vec::new();
    let Some(own) = pci(&root.join(primary)) else {
        return (rails, notes);
    };
    let mut candidates = Vec::new();
    for name in devices.iter().filter(|name| name.as_str() != primary) {
        let Some(other) = pci(&root.join(name)) else {
            continue;
        };
        if other.function == own.function
            && other.domain != own.domain
            && other.vendor == own.vendor
            && other.model == own.model
        {
            candidates.push((other.domain, name.clone()));
        }
    }
    candidates.sort();
    for (_, name) in candidates {
        let port_dir = root.join(&name).join("ports").join(port);
        let usable = (|| {
            if read(&port_dir.join("state")).ok()?.as_str() != "4: ACTIVE" {
                return Some(Err("its port is not active"));
            }
            let address: Ipv6Addr = read(&port_dir.join("gids").join(index))
                .ok()?
                .parse()
                .ok()?;
            if address
                .to_ipv4_mapped()
                .is_none_or(|v4| v4.is_unspecified())
            {
                return Some(Err("it has no IPv4 address at the GID index"));
            }
            if read(&port_dir.join("gid_attrs/types").join(index)).ok()? != "RoCE v2" {
                return Some(Err("its GID at that index is not RoCE v2"));
            }
            let interface = read(&port_dir.join("gid_attrs/ndevs").join(index)).ok()?;
            if !valid_interface(&interface) {
                return Some(Err("its GID at that index has no interface"));
            }
            Some(Ok(()))
        })();
        match usable {
            Some(Ok(())) if rails.len() + 1 < MAX_RAILS => rails.push(format!("{name}:{port}")),
            Some(Ok(())) => notes.push(format!("{name} left out: at most {MAX_RAILS} devices")),
            Some(Err(reason)) => notes.push(format!("{name} left out: {reason}")),
            None => notes.push(format!(
                "{name} left out: its RoCE state at GID index {index} could not be read"
            )),
        }
    }
    (rails, notes)
}

pub fn resolve(root: &Path, interface: &str, address: Ipv4Addr) -> Result<Binding, FabricError> {
    if !valid_interface(interface) {
        return Err(FabricError::Unavailable);
    }
    let mut matches = Vec::new();
    let mut devices = Vec::new();
    for device in entries(root, 32)? {
        let name = device
            .file_name()
            .into_string()
            .map_err(|_| FabricError::Unavailable)?;
        if name.is_empty()
            || name.len() > 64
            || !name
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
        {
            return Err(FabricError::Unavailable);
        }
        devices.push(name.clone());
        for port in entries(&device.path().join("ports"), 8)? {
            let number = port
                .file_name()
                .into_string()
                .map_err(|_| FabricError::Unavailable)?;
            let parsed: u8 = number.parse().map_err(|_| FabricError::Unavailable)?;
            if parsed == 0 || number != parsed.to_string() {
                return Err(FabricError::Unavailable);
            }
            if read(&port.path().join("state"))? != "4: ACTIVE" {
                continue;
            }
            for gid in entries(&port.path().join("gids"), 256)? {
                let index = gid
                    .file_name()
                    .into_string()
                    .map_err(|_| FabricError::Unavailable)?;
                let parsed: u16 = index.parse().map_err(|_| FabricError::Unavailable)?;
                if parsed > 255 || index != parsed.to_string() {
                    return Err(FabricError::Unavailable);
                }
                let gid_address: Ipv6Addr = read(&gid.path())?
                    .parse()
                    .map_err(|_| FabricError::Unavailable)?;
                if gid_address != address.to_ipv6_mapped() {
                    continue;
                }
                if read(&port.path().join("gid_attrs/types").join(&index))? != "RoCE v2"
                    || read(&port.path().join("gid_attrs/ndevs").join(&index))? != interface
                {
                    continue;
                }
                matches.push((name.clone(), number.clone(), index));
            }
        }
    }
    let [(device, port, index)] = matches.as_slice() else {
        return Err(FabricError::Unavailable);
    };
    devices.sort();
    let (extra, notes) = same_port_rails(root, &devices, device, port, index);
    let launch_hca = std::iter::once(format!("{device}:{port}"))
        .chain(extra)
        .collect::<Vec<_>>()
        .join(",");
    Ok(Binding {
        environment: vec![
            format!("NCCL_SOCKET_IFNAME=={interface}"),
            format!("NCCL_IB_HCA=={device}:{port}"),
            format!("NCCL_IB_GID_INDEX={index}"),
            format!("TP_SOCKET_IFNAME={interface}"),
            format!("GLOO_SOCKET_IFNAME={interface}"),
        ],
        launch_hca: format!("={launch_hca}"),
        notes,
    })
}

#[cfg(test)]
pub(crate) mod tests {
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

    /// A cabled QSFP port that reaches the host over two PCIe links.
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
}
