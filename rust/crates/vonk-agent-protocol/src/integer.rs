//! Lossless representation for canonically unbounded JSON integer tokens.
//!
//! Physical counters retain their own canonical bounds. This scalar is used
//! only where the schema permits integers without a finite machine domain.
use serde::{Deserialize, Deserializer, Serialize, Serializer, de::Error};
use serde_json::Number;
use std::fmt;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Integer(Number);

impl<'de> Deserialize<'de> for Integer {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let number = Number::deserialize(deserializer)?;
        if number.to_string().contains(['.', 'e', 'E']) {
            return Err(D::Error::custom("expected an integer token"));
        }
        Ok(Self(number))
    }
}

impl Serialize for Integer {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        self.0.serialize(serializer)
    }
}

impl fmt::Display for Integer {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

impl From<i64> for Integer {
    fn from(value: i64) -> Self {
        Self(value.into())
    }
}

impl From<u64> for Integer {
    fn from(value: u64) -> Self {
        Self(value.into())
    }
}
