//! Test listeners on loopback accept through these helpers.
//!
//! Some development VMs (OrbStack's port forwarder) probe every new loopback
//! listener with one `GET /` request from `Go-http-client`. A fixture that
//! accepts first and counts requests would take that probe for the client under
//! test. The probe is recognised by its `User-Agent` header and dropped; every
//! other connection, including a silent or empty one, is returned unchanged.

use std::io;
use std::net::SocketAddr;
use std::time::Duration;

const PROBE_AGENT: &[u8] = b"user-agent: go-http-client";
/// The probe sends its request at once; a client that is slower is not a probe.
const PROBE_WAIT: Duration = Duration::from_millis(250);
const PEEK_BYTES: usize = 512;

fn is_probe(peeked: &[u8]) -> bool {
    peeked.starts_with(b"GET / ")
        && peeked
            .windows(PROBE_AGENT.len())
            .any(|window| window.eq_ignore_ascii_case(PROBE_AGENT))
}

fn complete(peeked: &[u8]) -> bool {
    peeked.windows(4).any(|window| window == b"\r\n\r\n")
}

/// `TcpListener::accept` that drops port probes. A dropped probe reports the
/// listener's own next result, so a non-blocking caller sees `WouldBlock`.
pub(crate) fn accept(
    listener: &std::net::TcpListener,
) -> io::Result<(std::net::TcpStream, SocketAddr)> {
    loop {
        let (stream, address) = listener.accept()?;
        stream.set_nonblocking(true)?;
        let started = std::time::Instant::now();
        let mut buffer = [0_u8; PEEK_BYTES];
        let probe = loop {
            match stream.peek(&mut buffer) {
                Ok(0) => break false,
                Ok(size) if complete(&buffer[..size]) || size == PEEK_BYTES => {
                    break is_probe(&buffer[..size]);
                }
                Ok(_) => {}
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                Err(_) => break false,
            }
            if started.elapsed() >= PROBE_WAIT {
                break false;
            }
            std::thread::sleep(Duration::from_millis(2));
        };
        if probe {
            continue;
        }
        stream.set_nonblocking(false)?;
        return Ok((stream, address));
    }
}

/// `tokio::net::TcpListener::accept` that drops port probes.
pub(crate) async fn accept_async(
    listener: &tokio::net::TcpListener,
) -> io::Result<(tokio::net::TcpStream, SocketAddr)> {
    loop {
        let (stream, address) = listener.accept().await?;
        let mut buffer = [0_u8; PEEK_BYTES];
        let probe = tokio::time::timeout(PROBE_WAIT, async {
            loop {
                match stream.peek(&mut buffer).await {
                    Ok(size) if size == 0 || complete(&buffer[..size]) || size == PEEK_BYTES => {
                        return is_probe(&buffer[..size]);
                    }
                    Ok(_) => tokio::time::sleep(Duration::from_millis(2)).await,
                    Err(_) => return false,
                }
            }
        })
        .await
        .unwrap_or(false);
        if !probe {
            return Ok((stream, address));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    const PROBE: &[u8] = b"GET / HTTP/1.1\r\nHost: x\r\nUser-Agent: Go-http-client/1.1\r\n\r\n";

    #[test]
    fn a_go_probe_is_dropped_but_a_root_request_from_another_client_is_kept() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let mut probe = std::net::TcpStream::connect(address).unwrap();
        probe.write_all(PROBE).unwrap();
        let mut client = std::net::TcpStream::connect(address).unwrap();
        client
            .write_all(b"GET / HTTP/1.1\r\nUser-Agent: reqwest\r\n\r\n")
            .unwrap();
        let (_, peer) = accept(&listener).unwrap();
        assert_eq!(peer, client.local_addr().unwrap());
    }

    #[tokio::test]
    async fn a_silent_connection_is_returned_not_dropped() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let silent = tokio::net::TcpStream::connect(listener.local_addr().unwrap())
            .await
            .unwrap();
        let (_, peer) = accept_async(&listener).await.unwrap();
        assert_eq!(peer, silent.local_addr().unwrap());
    }
}
