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
pub(crate) mod tests;
