//! Streamed requests end on inactivity, never on a total.
//!
//! A small exchange carries its own total (`CONTROLLER_REQUEST_TIMEOUT`). A
//! request whose body or response streams is bounded only by an
//! [`Inactivity`] window that every moved byte restarts.

use super::*;

/// Send a streamed request: its head must arrive within one inactivity window
/// of the last byte that moved, however long the body took to send.
pub(super) async fn send_streaming(
    request: reqwest::RequestBuilder,
    inactivity: &Inactivity,
) -> Result<reqwest::Response, ClientError> {
    Ok(inactivity.bound(request.send()).await??)
}

/// The next body chunk within one inactivity window; its bytes restart it.
pub(super) async fn next_chunk<C: AsRef<[u8]>>(
    read: impl Future<Output = Result<Option<C>, reqwest::Error>>,
    inactivity: &Inactivity,
) -> Result<Option<C>, ClientError> {
    let chunk = inactivity.bound(read).await??;
    if let Some(chunk) = &chunk {
        inactivity.observe_bytes(chunk.as_ref().len() as u64);
    }
    Ok(chunk)
}
