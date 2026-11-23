#![cfg(test)]
use super::*;

#[test]
fn allocation_takes_the_first_free_range() {
    for (entries, expected) in [
        ("", Some((100_000, 165_535))),
        ("carst:100000:65536\n", Some((165_536, 231_071))),
        (
            "first:165536:65536\nlast:100000:65536\n",
            Some((231_072, 296_607)),
        ),
        (
            "first:100000:32768\nsecond:198304:65536\n",
            Some((132_768, 198_303)),
        ),
        // An entry starting inside the window pushes the candidate past it.
        ("inside:120000:1000\n", Some((121_000, 186_535))),
    ] {
        assert_eq!(allocate(entries, 100_000, 600_100_000, 65_536), expected);
    }
}

#[test]
fn allocation_reports_an_exhausted_space() {
    assert_eq!(
        allocate(
            "first:100000:65536\nsecond:165536:65536\n",
            100_000,
            231_071,
            65_536
        ),
        None
    );
}

#[test]
fn an_account_with_enough_ids_needs_no_range() {
    assert!(holds("vonk-agent:100000:65536\n", 999, 65_536));
    assert!(holds("999:100000:65536\n", 999, 65_536));
    assert!(!holds("other:100000:65536\nvonk-agent:1:10\n", 999, 65_536));
}
