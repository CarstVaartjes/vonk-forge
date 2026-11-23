    use super::{MAX_ARGV_BYTES, MAX_ARGV_ITEM_BYTES, MAX_ARGV_ITEMS, valid_opaque_argv};

    #[test]
    fn a_long_command_is_admitted_and_an_absurd_one_is_refused() {
        // Wrong implementation: MAX_ARGV_ITEMS = 512 refused a 600-element
        // engine command while the derived argv byte ceiling was nowhere near.
        let long: Vec<String> = (0..600).map(|index| format!("--flag-{index}")).collect();
        assert!(valid_opaque_argv(&long));

        let absurd: Vec<String> = (0..MAX_ARGV_ITEMS + 1)
            .map(|index| format!("--flag-{index}"))
            .collect();
        assert!(!valid_opaque_argv(&absurd));
    }

    #[test]
    fn the_argv_byte_budget_is_admitted_exactly_and_refused_one_byte_over() {
        // Wrong implementation: this budget was `1024 * 1024`, exactly the
        // helper frame budget, while its comment called it a backstop *below*
        // that budget -- so an argv the plan admitted could still be
        // unframeable by the request that has to carry it. The envelope test in
        // `lib.rs` proves the budget still derives below that request ceiling.
        // The item ceiling is separate, so reach this total in item-sized
        // pieces.
        let mut exact = vec!["x".repeat(MAX_ARGV_ITEM_BYTES); MAX_ARGV_BYTES / MAX_ARGV_ITEM_BYTES];
        exact.push("x".repeat(MAX_ARGV_BYTES % MAX_ARGV_ITEM_BYTES));
        assert_eq!(exact.iter().map(String::len).sum::<usize>(), MAX_ARGV_BYTES);
        assert!(valid_opaque_argv(&exact));
        exact.push("x".to_owned());
        assert!(!valid_opaque_argv(&exact));
    }
