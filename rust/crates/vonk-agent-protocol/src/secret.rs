//! A wire string whose value is never printed.
//!
//! The generated wire types derive `Debug`, so a secret field must be a type
//! whose `Debug` redacts it (the Python model marks the same field
//! `repr=False`). It serializes as the plain string; read it with `Deref`.
use std::fmt;
use std::ops::Deref;

#[derive(Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(transparent)]
pub struct Secret(String);

impl Deref for Secret {
    type Target = str;

    fn deref(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Secret(<redacted>)")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debug_never_prints_the_value_but_the_wire_carries_it() {
        let secret: Secret = serde_json::from_str("\"value-on-the-wire\"").unwrap();
        assert!(!format!("{secret:?}").contains("value-on-the-wire"));
        assert_eq!(&*secret, "value-on-the-wire");
        assert_eq!(
            serde_json::to_string(&secret).unwrap(),
            "\"value-on-the-wire\""
        );
    }
}
