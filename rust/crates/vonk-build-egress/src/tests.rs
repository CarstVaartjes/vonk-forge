use super::*;

#[test]
fn silent_probe_ends_and_a_fresh_probe_completes() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let client = TcpStream::connect(address).unwrap();
    let (silent, _) = listener.accept().unwrap();
    thread::spawn(move || {
        thread::sleep(Duration::from_millis(250));
        drop(silent);
    });
    let started = Instant::now();
    assert!(probe(client, started + Duration::from_millis(50)).is_err());
    assert!(started.elapsed() < Duration::from_millis(200));
    let client = TcpStream::connect(address).unwrap();
    let (mut server, _) = listener.accept().unwrap();
    server.write_all(b"HTTP/1.1 403 Forbidden\r\n").unwrap();
    probe(client, Instant::now() + Duration::from_secs(1)).unwrap();
}

#[test]
fn a_retained_resolver_writer_cannot_extend_the_deadline() {
    let (mut reader, retained_writer) = UnixStream::pair().unwrap();
    thread::spawn(move || {
        thread::sleep(Duration::from_millis(250));
        drop(retained_writer);
    });
    let started = Instant::now();
    assert!(read_resolver_output(&mut reader, started + Duration::from_millis(50)).is_err());
    assert!(started.elapsed() < Duration::from_secs(1));
    let (mut reader, mut writer) = UnixStream::pair().unwrap();
    writer.write_all(b"1.1.1.1:443\n").unwrap();
    drop(writer);
    assert_eq!(
        read_resolver_output(&mut reader, Instant::now() + Duration::from_secs(1)).unwrap(),
        b"1.1.1.1:443\n"
    );
}

#[test]
fn stalled_resolver_is_reaped_and_fresh_work_is_admitted() {
    let temp = tempfile::tempdir().unwrap();
    let pidfile = temp.path().join("resolver.pid");
    for _ in 0..4 {
        let started = Instant::now();
        assert!(
            resolver_output(
                Command::new("/bin/sh")
                    .args(["-c", "echo $$ >\"$1\"; exec sleep 10", "resolver"])
                    .arg(&pidfile),
                started + Duration::from_millis(100)
            )
            .is_err()
        );
        assert!(started.elapsed() < Duration::from_secs(1));
        let pid = std::fs::read_to_string(&pidfile).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        // kill -0 remains successful for a zombie. Failure therefore
        // proves both termination and removal from the process table.
        while Command::new("/bin/kill")
            .args(["-0", pid.trim()])
            .stderr(Stdio::null())
            .status()
            .unwrap()
            .success()
        {
            assert!(Instant::now() < deadline, "resolver was abandoned");
            thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(
            resolver_output(
                Command::new("/bin/echo").arg("1.1.1.1:443"),
                Instant::now() + Duration::from_secs(1)
            )
            .unwrap(),
            b"1.1.1.1:443\n"
        );
    }
}

#[test]
fn handle_reobserves_dns_unknowns_then_forwards_and_admits_fresh_requests() {
    let hosts = BTreeSet::from(["allowed.example".to_owned()]);
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    for replies in [
        vec![
            Ok(Vec::new()),
            Ok(b"malformed".to_vec()),
            Ok(b"1.1.1.1:80\n".to_vec()),
        ],
        vec![Err(()), Err(()), Err(())],
        vec![Ok(b"169.254.169.254:80\n".to_vec())],
        vec![Ok(b"1.1.1.1:80\n".to_vec())],
    ] {
        let expected_attempts = replies.len();
        let successful = replies
            .last()
            .unwrap()
            .as_ref()
            .is_ok_and(|v| v == b"1.1.1.1:80\n");
        let denied = replies
            .last()
            .unwrap()
            .as_ref()
            .is_ok_and(|v| v == b"169.254.169.254:80\n");
        let mut client = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        client
            .write_all(b"GET http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\n\r\n")
            .unwrap();
        let (server, _) = listener.accept().unwrap();
        let mut replies = replies.into_iter();
        let mut observed = 0;
        let mut connected = false;
        handle_observed(
            server,
            &hosts,
            |_, port| {
                resolve_observed(port, Instant::now() + Duration::from_secs(1), |_| {
                    observed += 1;
                    replies.next().unwrap()
                })
            },
            |addresses| {
                assert_eq!(addresses, &["1.1.1.1:80".parse::<SocketAddr>().unwrap()]);
                connected = true;
                let upstream = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
                let (mut peer, _) = listener.accept().unwrap();
                thread::spawn(move || {
                    let request = read_header(&mut peer).unwrap();
                    assert!(request.starts_with(b"GET / HTTP/1.1\r\n"));
                    peer.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                        .unwrap();
                });
                Ok(upstream)
            },
        )
        .unwrap();
        let mut response = Vec::new();
        client.read_to_end(&mut response).unwrap();
        assert_eq!(observed, expected_attempts);
        assert_eq!(connected, successful);
        let status = if successful {
            b"200"
        } else if denied {
            b"403"
        } else {
            b"502"
        };
        assert_eq!(&response[9..12], status);
    }
}

#[test]
fn probe_retries_malformed_replies_ends_and_admits_a_fresh_probe() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let (done_tx, done_rx) = std::sync::mpsc::channel();
    thread::spawn(move || {
        for reply in [
            b"broken reply".as_slice(),
            b"".as_slice(),
            b"HTTP/1.1 403 Forbidden\r\n".as_slice(),
            b"HTTP/1.1 403 Forbidden\r\n".as_slice(),
        ] {
            let (mut stream, _) = listener.accept().unwrap();
            read_header(&mut stream).unwrap();
            stream.write_all(reply).unwrap();
        }
        done_tx.send(()).unwrap();
    });
    probe_available(address, Instant::now() + Duration::from_secs(2)).unwrap();
    probe_available(address, Instant::now() + Duration::from_secs(2)).unwrap();
    done_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    let unavailable = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = unavailable.local_addr().unwrap();
    drop(unavailable);
    let start = Instant::now();
    assert!(probe_available(address, start + Duration::from_millis(100)).is_err());
    assert!(start.elapsed() < Duration::from_secs(1));
    let listener = TcpListener::bind(address).unwrap();
    let (done_tx, done_rx) = std::sync::mpsc::channel();
    thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        read_header(&mut stream).unwrap();
        stream.write_all(b"HTTP/1.1 403 Forbidden\r\n").unwrap();
        done_tx.send(()).unwrap();
    });
    probe_available(address, Instant::now() + Duration::from_secs(1)).unwrap();
    done_rx.recv_timeout(Duration::from_secs(2)).unwrap();
}

#[test]
fn rejects_private_reserved_and_metadata_destinations() {
    for value in [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "100.64.0.1",
        "192.0.2.1",
        "192.88.99.1",
        "198.18.0.1",
        "198.51.100.1",
        "203.0.113.1",
        "::1",
        "64:ff9b::1",
        "100::1",
        "fd00::1",
        "fe80::1",
        "2001::1",
        "2001:db8::1",
        "2002::1",
        "3fff::1",
        "5f00::1",
    ] {
        assert!(!public_ip(value.parse().unwrap()), "accepted {value}");
    }
    assert!(public_ip("1.1.1.1".parse().unwrap()));
    assert!(public_ip("2606:4700:4700::1111".parse().unwrap()));
    assert!(blocked_metadata_name("metadata.google.internal"));
}

#[test]
fn dns_rebinding_or_mixed_answers_fail_closed() {
    let addresses = BTreeSet::from([
        "1.1.1.1:443".parse().unwrap(),
        "169.254.169.254:443".parse().unwrap(),
    ]);
    assert!(validate_resolved(addresses).is_err());
}

#[test]
fn exact_allowlist_ports_and_safe_http_methods_are_enforced() {
    let hosts = BTreeSet::from(["pypi.org".to_owned()]);
    assert!(
        parse_request(
            b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n",
            &hosts
        )
        .is_ok()
    );
    assert_eq!(
        parse_request(
            b"CONNECT files.pythonhosted.org:443 HTTP/1.1\r\n\r\n",
            &hosts
        )
        .unwrap_err(),
        403
    );
    assert_eq!(
        parse_request(b"CONNECT pypi.org:22 HTTP/1.1\r\n\r\n", &hosts).unwrap_err(),
        403
    );
    assert_eq!(
        parse_request(
            b"POST http://pypi.org/upload HTTP/1.1\r\nHost: pypi.org\r\n\r\n",
            &hosts
        )
        .unwrap_err(),
        405
    );
}

#[test]
fn strips_hop_headers_and_rejects_proxy_credentials_and_bodies() {
    let hosts = BTreeSet::from(["pypi.org".to_owned()]);
    let request = parse_request(b"GET http://pypi.org/simple HTTP/1.1\r\nHost: pypi.org\r\nProxy-Connection: keep-alive\r\nConnection: upgrade, X-Secret\r\nUpgrade: websocket\r\nX-Secret: remove-me\r\nUser-Agent: test\r\n\r\n", &hosts).unwrap();
    let text = String::from_utf8(request.forward).unwrap();
    assert!(!text.to_ascii_lowercase().contains("proxy-connection"));
    assert!(!text.to_ascii_lowercase().contains("upgrade"));
    assert!(!text.to_ascii_lowercase().contains("x-secret"));
    assert!(text.contains("User-Agent: test"));
    assert_eq!(parse_request(b"GET http://pypi.org/ HTTP/1.1\r\nHost: pypi.org\r\nProxy-Authorization: Basic abc\r\n\r\n", &hosts).unwrap_err(), 403);
    assert_eq!(
        parse_request(
            b"GET http://pypi.org/ HTTP/1.1\r\nHost: pypi.org\r\nContent-Length: 1\r\n\r\n",
            &hosts
        )
        .unwrap_err(),
        413
    );
    assert_eq!(parse_request(b"GET http://pypi.org/ HTTP/1.1\r\nHost: pypi.org\r\nTransfer-Encoding: chunked\r\n\r\n", &hosts).unwrap_err(), 413);
}

#[test]
fn failed_termination_retains_child_and_exhausted_owners_recover_after_reaping() {
    // Wrong implementation: dropping a child after failed kill releases its
    // claim, lets repeated timeouts accumulate processes, or leaves a zombie.
    static OWNERS: AtomicUsize = AtomicUsize::new(0);
    let mut owners = (0..MAX_CONNECTIONS)
        .map(|_| ResolverOwner::acquire_at(&OWNERS).unwrap())
        .collect::<Vec<_>>();
    assert!(ResolverOwner::acquire_at(&OWNERS).is_err());
    let child = Command::new("/bin/sleep").arg("0.35").spawn().unwrap();
    let pid = child.id().to_string();
    let owner = owners.pop().unwrap();
    let started = Instant::now();
    retain_resolver(child, owner, |_| { /* simulate unavailable termination */ });
    assert!(started.elapsed() < Duration::from_millis(100));
    assert!(ResolverOwner::acquire_at(&OWNERS).is_err());
    let deadline = Instant::now() + Duration::from_secs(3);
    while OWNERS.load(Ordering::Acquire) == MAX_CONNECTIONS {
        assert!(Instant::now() < deadline, "late child was not reaped");
        thread::sleep(Duration::from_millis(10));
    }
    assert!(
        !Command::new("/bin/kill")
            .args(["-0", &pid])
            .stderr(Stdio::null())
            .status()
            .unwrap()
            .success()
    );
    let fresh = ResolverOwner::acquire_at(&OWNERS).unwrap();
    assert_eq!(
        resolver_output(
            Command::new("/bin/echo").arg("1.1.1.1:443"),
            Instant::now() + Duration::from_secs(1)
        )
        .unwrap(),
        b"1.1.1.1:443\n"
    );
    drop(fresh);
    drop(owners);
    assert_eq!(OWNERS.load(Ordering::Acquire), 0);
}

#[test]
fn exhausted_reap_budget_hands_actual_child_to_supervision_and_fresh_admission_recovers() {
    // The old per-child infinite loop never reached a terminal ownership handoff.
    static OWNERS: AtomicUsize = AtomicUsize::new(0);
    let owner = ResolverOwner::acquire_at(&OWNERS).unwrap();
    let child = Command::new("/bin/sleep").arg("30").spawn().unwrap();
    let pid = child.id();
    retain_resolver_until(
        child,
        owner,
        |_| {},
        Instant::now() + Duration::from_millis(30),
    );
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let handed_off = RETAINED_RESOLVERS
            .lock()
            .unwrap()
            .iter()
            .any(|(child, _)| child.id() == pid);
        if handed_off {
            break;
        }
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(OWNERS.load(Ordering::Acquire), 1);
    while OWNERS.load(Ordering::Acquire) != 0 {
        assert!(Instant::now() < deadline);
        supervise_retained_resolvers();
        thread::sleep(Duration::from_millis(10));
    }
    let fresh = ResolverOwner::acquire_at(&OWNERS).unwrap();
    assert_eq!(
        resolver_output(
            Command::new("/bin/echo").arg("1.1.1.1:443"),
            Instant::now() + Duration::from_secs(1)
        )
        .unwrap(),
        b"1.1.1.1:443\n"
    );
    drop(fresh);
    assert_eq!(OWNERS.load(Ordering::Acquire), 0);
}
