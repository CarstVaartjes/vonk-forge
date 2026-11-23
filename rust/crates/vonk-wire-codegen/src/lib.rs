#![expect(
    clippy::unwrap_used,
    reason = "build-time generator: a panic fails the build"
)]
mod declarations;
mod deserialize;
mod keywords;
mod render;
mod schema;

pub use render::render;

#[cfg(test)]
mod tests;
