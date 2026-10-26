//! Standalone static executable for the real rootless Dockerfile RUN regression.
#![forbid(unsafe_code)]

use std::{
    env, fs,
    io::{Read, Write},
    net::{SocketAddr, TcpStream, ToSocketAddrs},
    time::Duration,
};

fn main() {
    if env::args().nth(1).as_deref() == Some("--reachable") {
        let address: SocketAddr = env::args().nth(2).unwrap().parse().unwrap();
        TcpStream::connect_timeout(&address, Duration::from_secs(2)).unwrap();
        return;
    }
    if env::args().nth(1).as_deref() == Some("--hold-tunnels") {
        let proxy: SocketAddr = env::args().nth(2).unwrap().parse().unwrap();
        let count: usize = env::args().nth(3).unwrap().parse().unwrap();
        hold_tunnels(proxy, count, &env::args().nth(4).unwrap());
        return;
    }
    let status = fs::read_to_string("/proc/self/status").unwrap();
    assert!(status.lines().any(|line| line == "NoNewPrivs:\t1"));
    assert!(
        status
            .lines()
            .any(|line| line == "CapEff:\t0000000000000000")
    );
    let outside: SocketAddr = env::var("VONK_TEST_OUTBOUND").unwrap().parse().unwrap();
    assert!(
        TcpStream::connect_timeout(&outside, Duration::from_secs(1)).is_err(),
        "the build bypassed its internal-only network"
    );
    if env::var("VONK_TEST_NETWORK").unwrap() == "public" {
        let proxy = env::var("HTTP_PROXY").expect("public builds must reach the approved proxy");
        let authority = proxy.strip_prefix("http://").unwrap();
        let address = authority.to_socket_addrs().unwrap().next().unwrap();
        let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2)).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        stream
            .set_write_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        stream
            .write_all(b"GET http://proxy.invalid/ HTTP/1.1\r\nHost: proxy.invalid\r\n\r\n")
            .unwrap();
        let mut response = [0; 12];
        stream.read_exact(&mut response).unwrap();
        assert_eq!(&response, b"HTTP/1.1 403");
    } else {
        assert!(env::var("HTTP_PROXY").is_err());
    }
    println!("Dockerfile RUN retained the network and privilege boundary");
}

/// Open `count` CONNECT tunnels at once and hold them, as `uv` does, then show
/// the proxy still answers a further connection (503 at its connection bound).
fn hold_tunnels(proxy: SocketAddr, count: usize, authority: &str) {
    let mut tunnels = Vec::new();
    for _ in 0..count {
        let mut stream = TcpStream::connect_timeout(&proxy, Duration::from_secs(5)).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(15)))
            .unwrap();
        write!(stream, "CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n").unwrap();
        let response = read_reply(&mut stream);
        assert!(
            response.starts_with(b"HTTP/1.1 200"),
            "tunnel {} refused: {}",
            tunnels.len(),
            String::from_utf8_lossy(&response)
        );
        tunnels.push(stream);
    }
    // Every tunnel must still be open: a proxy that dropped one would show EOF,
    // an idle healthy tunnel only times out.
    for (index, tunnel) in tunnels.iter_mut().enumerate() {
        tunnel
            .set_read_timeout(Some(Duration::from_millis(50)))
            .unwrap();
        let mut byte = [0; 1];
        match tunnel.read(&mut byte) {
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) => {}
            other => panic!("tunnel {index} did not stay open: {other:?}"),
        }
    }
    let mut extra = TcpStream::connect_timeout(&proxy, Duration::from_secs(5)).unwrap();
    extra.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    write!(extra, "CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n").unwrap();
    let response = read_reply(&mut extra);
    assert!(response.starts_with(b"HTTP/1.1 "));
    println!(
        "held {} tunnels; proxy answered {}",
        tunnels.len(),
        String::from_utf8_lossy(&response[..12])
    );
}

/// The proxy's reply head, through its blank line, so a later read sees only
/// tunnel bytes.
fn read_reply(stream: &mut TcpStream) -> Vec<u8> {
    let mut reply = Vec::new();
    let mut byte = [0; 1];
    while !reply.ends_with(b"\r\n\r\n") {
        stream
            .read_exact(&mut byte)
            .expect("the proxy stopped answering while holding tunnels");
        reply.push(byte[0]);
    }
    reply
}
