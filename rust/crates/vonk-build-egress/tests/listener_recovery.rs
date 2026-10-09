//! A local bind fault must not permanently terminate the build proxy.
use std::{
    net::{TcpListener, TcpStream},
    process::{Child, Command},
    thread,
    time::{Duration, Instant},
};

struct Proxy(Child);
impl Drop for Proxy {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn occupied_listener_recovers_and_accepts_a_fresh_connection() {
    let occupied = TcpListener::bind("0.0.0.0:18080").unwrap();
    let mut proxy = Proxy(
        Command::new(env!("CARGO_BIN_EXE_vonk-build-egress"))
            .args(["--allow-host", "example.com"])
            .spawn()
            .unwrap(),
    );
    thread::sleep(Duration::from_millis(250));
    assert!(proxy.0.try_wait().unwrap().is_none());
    drop(occupied);
    let deadline = Instant::now() + Duration::from_secs(8);
    while Instant::now() < deadline {
        if TcpStream::connect_timeout(
            &"127.0.0.1:18080".parse().unwrap(),
            Duration::from_millis(100),
        )
        .is_ok()
        {
            assert!(proxy.0.try_wait().unwrap().is_none());
            return;
        }
        thread::sleep(Duration::from_millis(50));
    }
    panic!("proxy did not recover its listener after the local fault cleared");
}
