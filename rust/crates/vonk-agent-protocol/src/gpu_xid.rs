//! The NVIDIA Xid code a kernel or `nvidia-smi` line names.
//!
//! One parser for two readers: the agent's reset-class check and the helper's
//! exit evidence. Line format: the NVIDIA Xid catalog,
//! <https://docs.nvidia.com/deploy/xid-errors/analyzing-xid-catalog.html>.

/// The code in `NVRM: Xid (PCI:000f:01:00): 31, pid=...` (the PCI part is
/// optional, and the match is case-insensitive), or `None` when the line names
/// no Xid code.
pub fn xid_code(line: &str) -> Option<u32> {
    let line = line.to_ascii_lowercase();
    let (_, value) = line.split_once("xid")?;
    let value = value.trim_start();
    let value = if value.starts_with('(') {
        value.split_once(')')?.1
    } else {
        value
    };
    value
        .trim_start_matches(|c: char| c.is_whitespace() || c == ':')
        .split(|c: char| !c.is_ascii_digit())
        .next()
        .and_then(|code| code.parse().ok())
}

#[cfg(test)]
mod tests {
    use super::xid_code;

    #[test]
    fn the_code_is_read_from_a_kernel_line() {
        // Lines follow the NVIDIA Xid catalog format; no real Xid line exists in
        // a Spark's retained journal.
        let classic = "NVRM: Xid (PCI:0000:01:00): 31, pid=1234, name=python, Ch 00000008";
        let gb10 = "NVRM: Xid (PCI:000f:01:00): 43, pid=77, name=python, Ch 00000010";
        let other = "NVRM: GPU at PCI:000f:01:00: GPU-1234 allocation failed";
        assert_eq!(xid_code(classic), Some(31));
        assert_eq!(xid_code(gb10), Some(43));
        assert_eq!(xid_code(other), None);
    }
}
