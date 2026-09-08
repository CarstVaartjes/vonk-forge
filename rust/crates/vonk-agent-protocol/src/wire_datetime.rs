//! Native Pydantic datetime representation, separate from formatted strings.
use chrono::{DateTime, FixedOffset, SecondsFormat, Timelike};
use serde::{Deserialize, Deserializer, Serializer, de::Error as _, ser::Error as _};

fn normalize(value: DateTime<FixedOffset>) -> Result<DateTime<FixedOffset>, &'static str> {
    // Python datetime has microsecond precision and does not represent leap seconds.
    if value.nanosecond() >= 1_000_000_000 {
        return Err("native wire datetime cannot contain a leap second");
    }
    value
        .with_nanosecond(value.nanosecond() / 1_000 * 1_000)
        .ok_or("native wire datetime precision is invalid")
}

pub(crate) fn serialize<S: Serializer>(
    value: &DateTime<FixedOffset>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    let value = normalize(*value).map_err(S::Error::custom)?;
    let precision = if value.timestamp_subsec_micros() == 0 {
        SecondsFormat::Secs
    } else {
        SecondsFormat::Micros
    };
    serializer.serialize_str(&value.to_rfc3339_opts(precision, true))
}

pub(crate) fn deserialize<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<DateTime<FixedOffset>, D::Error> {
    normalize(DateTime::<FixedOffset>::deserialize(deserializer)?).map_err(D::Error::custom)
}

pub(crate) fn serialize_optional<S: Serializer>(
    value: &Option<DateTime<FixedOffset>>,
    serializer: S,
) -> Result<S::Ok, S::Error> {
    match value {
        Some(value) => serialize(value, serializer),
        None => serializer.serialize_none(),
    }
}

pub(crate) fn deserialize_optional<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<DateTime<FixedOffset>>, D::Error> {
    Option::<DateTime<FixedOffset>>::deserialize(deserializer)?
        .map(normalize)
        .transpose()
        .map_err(D::Error::custom)
}
