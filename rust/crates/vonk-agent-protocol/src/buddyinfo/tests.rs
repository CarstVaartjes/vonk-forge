#![cfg(test)]
use super::*;

const BUDDYINFO: &str = "\
Node 0, zone      DMA      1      0      0      0      0      0      0      0      0      1      3
Node 0, zone   Normal    100     50      0      0      0      0      0      0      0      2      0
";

#[test]
fn blocks_of_2_mib_follow_the_kernel_page_size() {
    // 4 KiB pages: order 9 and up are 2 MiB blocks.
    let small = free_memory(BUDDYINFO, 4096).unwrap();
    assert_eq!(
        small.free_bytes,
        4096 * (1 + 100 + 2 * 50) + (3 << 21) + (3 << 22)
    );
    assert_eq!(small.free_2mib_block_bytes, (3 << 21) + (3 << 22));
    // 64 KiB pages: order 5 and up.
    let large = free_memory("Node 0, zone Normal 0 0 0 0 1 2\n", 65536).unwrap();
    assert_eq!(large.free_bytes, (1 << 20) + (2 << 21));
    assert_eq!(large.free_2mib_block_bytes, 2 << 21);
}

#[test]
fn text_that_is_not_buddyinfo_is_unknown_not_zero() {
    assert!(free_memory("", 4096).is_none());
    assert!(free_memory("Node 0, zone Normal 1 x\n", 4096).is_none());
    assert!(free_memory("garbage\n", 4096).is_none());
}
