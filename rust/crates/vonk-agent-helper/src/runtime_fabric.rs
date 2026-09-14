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

pub fn resolve(
    root: &Path,
    interface: &str,
    address: Ipv4Addr,
) -> Result<Vec<String>, FabricError> {
    if !valid_interface(interface) {
        return Err(FabricError::Unavailable);
    }
    let mut matches = Vec::new();
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
    Ok(vec![
        format!("NCCL_SOCKET_IFNAME=={interface}"),
        format!("NCCL_IB_HCA=={device}:{port}"),
        format!("NCCL_IB_GID_INDEX={index}"),
        format!("TP_SOCKET_IFNAME={interface}"),
        format!("GLOO_SOCKET_IFNAME={interface}"),
    ])
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
        let env = resolve(root.path(), "enp1s0f1np1", address).unwrap();
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
