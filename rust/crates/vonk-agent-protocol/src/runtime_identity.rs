//! Platform-owned NSS identity for numeric container users, independent of images.
use std::io::{self, Write};

/// No host accounts or credentials enter a workload. The runtime owns its
/// numeric identity and writable home; root remains discoverable for tools.
pub fn files(uid: u32, gid: u32) -> [(&'static str, String); 2] {
    [
        (
            "etc/passwd",
            format!(
                "root:x:0:0:root:/root:/bin/false\nvonk-runtime:x:{uid}:{gid}:Vonk runtime:/outputs/cache/home:/bin/false\n"
            ),
        ),
        ("etc/group", format!("root:x:0:\nvonk-runtime:x:{gid}:\n")),
    ]
}

/// Scratch images used by build and runtime probes have no base NSS files.
pub fn append_to<W: Write>(archive: &mut tar::Builder<W>, uid: u32, gid: u32) -> io::Result<()> {
    for (path, content) in files(uid, gid) {
        let mut header = tar::Header::new_gnu();
        header.set_size(content.len() as u64);
        header.set_mode(0o444);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_cksum();
        archive.append_data(&mut header, path, content.as_bytes())?;
    }
    Ok(())
}
