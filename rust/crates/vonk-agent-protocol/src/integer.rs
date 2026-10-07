//! Lossless representation for canonically unbounded JSON integer tokens.
//!
//! Fields with proven physical/owner limits retain their canonical bounds.
//! This scalar is used when the schema has no finite machine domain; schema
//! validation still applies any declared minimum at the model boundary.
use std::cmp::Ordering;
use std::fmt;

pub use crate::generated::Integer;

impl Default for Integer {
    fn default() -> Self {
        Self::from(0_u64)
    }
}

impl Ord for Integer {
    fn cmp(&self, other: &Self) -> Ordering {
        let left = self.number().to_string();
        let right = other.number().to_string();
        let left_negative = left.starts_with('-');
        let right_negative = right.starts_with('-');
        if left_negative != right_negative {
            return if left_negative {
                Ordering::Less
            } else {
                Ordering::Greater
            };
        }
        let left = left.trim_start_matches('-');
        let right = right.trim_start_matches('-');
        let magnitude = left.len().cmp(&right.len()).then_with(|| left.cmp(right));
        if left_negative {
            magnitude.reverse()
        } else {
            magnitude
        }
    }
}

impl PartialOrd for Integer {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl PartialEq<u64> for Integer {
    fn eq(&self, other: &u64) -> bool {
        self == &Self::from(*other)
    }
}

impl PartialOrd<u64> for Integer {
    fn partial_cmp(&self, other: &u64) -> Option<Ordering> {
        Some(self.cmp(&Self::from(*other)))
    }
}

impl fmt::Display for Integer {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.number().fmt(formatter)
    }
}

impl From<i64> for Integer {
    fn from(value: i64) -> Self {
        Self::from_i64(value)
    }
}

impl From<u64> for Integer {
    fn from(value: u64) -> Self {
        Self::from_u64(value)
    }
}
