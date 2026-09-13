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
