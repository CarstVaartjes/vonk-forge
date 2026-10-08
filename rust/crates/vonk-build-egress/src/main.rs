#![forbid(unsafe_code)]

use std::{
    collections::BTreeSet,
    io::{self, Read, Write},
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, TcpListener, TcpStream, ToSocketAddrs},
    os::{fd::OwnedFd, unix::net::UnixStream},
    process::{Command, ExitCode, Stdio},
    sync::{
        Arc,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

const LISTEN: &str = "0.0.0.0:18080";
const MAX_HEADER_BYTES: usize = 16 * 1024;
const MAX_CONNECTIONS: usize = 64;
const MAX_RESOLVED_ADDRESSES: usize = 16;
const MAX_TUNNEL_BYTES: u64 = 16 * 1024 * 1024 * 1024;
/// Byte budget for one connection, shared across both copy directions so a
/// tunnel cannot move MAX_TUNNEL_BYTES in each direction.
const IO_TIMEOUT: Duration = Duration::from_secs(120);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(15);

fn main() -> ExitCode {
    match run(std::env::args().skip(1).collect()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("vonk-build-egress: {error}");
            ExitCode::from(2)
        }
    }
}

fn run(arguments: Vec<String>) -> Result<(), String> {
    if arguments == ["--probe"] {
        let deadline = Instant::now() + Duration::from_secs(2);
        let stream = TcpStream::connect_timeout(
            &"127.0.0.1:18080"
                .parse()
                .map_err(|_| "probe address is invalid")?,
            deadline.saturating_duration_since(Instant::now()),
        )
        .map_err(|_| "proxy is unavailable")?;
        probe(stream, deadline).map_err(|_| "proxy probe unavailable")?;
        return Ok(());
    }
    // Isolate the system resolver in a killable process. A stalled libc/NSS
    // lookup cannot retain a worker or accumulate abandoned resolver threads.
    if let [mode, host, port] = arguments.as_slice()
        && mode == "--resolve"
    {
        let port = port
            .parse::<u16>()
            .map_err(|_| "resolver port is invalid")?;
        let addresses = (host.as_str(), port)
            .to_socket_addrs()
            .map_err(|_| "resolver unavailable")?;
        for (index, address) in addresses.enumerate() {
            if index >= MAX_RESOLVED_ADDRESSES {
                return Err("resolver answer exceeds bound".to_owned());
            }
            println!("{address}");
        }
        return Ok(());
    }
    let mut hosts = BTreeSet::new();
    let mut index = 0;
    while index < arguments.len() {
        if arguments[index] != "--allow-host" || index + 1 >= arguments.len() {
            return Err("usage: vonk-build-egress --allow-host HOST ...".to_owned());
        }
        let host = arguments[index + 1].clone();
        if !valid_hostname(&host) || blocked_metadata_name(&host) || !hosts.insert(host) {
            return Err("declared host allowlist is invalid".to_owned());
        }
        index += 2;
    }
    if hosts.is_empty() || hosts.len() > 64 {
        return Err("declared host allowlist is invalid".to_owned());
    }
    serve(Arc::new(hosts)).map_err(|_| "proxy listener failed".to_owned())
}

fn serve(hosts: Arc<BTreeSet<String>>) -> io::Result<()> {
    let listener = TcpListener::bind(LISTEN)?;
    let active = Arc::new(AtomicUsize::new(0));
    for incoming in listener.incoming() {
        let Ok(stream) = incoming else { continue };
        if active.fetch_add(1, Ordering::AcqRel) >= MAX_CONNECTIONS {
            active.fetch_sub(1, Ordering::AcqRel);
            reject(stream, 503);
            continue;
        }
        let active = Arc::clone(&active);
        let hosts = Arc::clone(&hosts);
        thread::spawn(move || {
            let _guard = ConnectionGuard(active);
            let _ = handle(stream, &hosts);
        });
    }
    Ok(())
}

struct ConnectionGuard(Arc<AtomicUsize>);
impl Drop for ConnectionGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}

fn handle(mut client: TcpStream, hosts: &BTreeSet<String>) -> io::Result<()> {
    client.set_read_timeout(Some(IO_TIMEOUT))?;
    client.set_write_timeout(Some(IO_TIMEOUT))?;
    let header = read_header(&mut client)?;
    let request = match parse_request(&header, hosts) {
        Ok(request) => request,
        Err(status) => {
            reject(client, status);
            return Ok(());
        }
    };
    let addresses = match resolve_public(&request.host, request.port) {
        Ok(value) => value,
        Err(status) => {
            reject(client, status);
            return Ok(());
        }
    };
    let mut upstream = match connect_any(&addresses) {
        Ok(value) => value,
        Err(()) => {
            reject(client, 502);
            return Ok(());
        }
    };
    upstream.set_read_timeout(Some(IO_TIMEOUT))?;
    upstream.set_write_timeout(Some(IO_TIMEOUT))?;
    if request.connect {
        client.write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n")?;
        tunnel(client, upstream)
    } else {
        upstream.write_all(&request.forward)?;
        copy_bounded(&mut upstream, &mut client, &AtomicU64::new(0)).map(|_| ())
    }
}

#[derive(Debug)]
struct Request {
    host: String,
    port: u16,
    connect: bool,
    forward: Vec<u8>,
}

fn parse_request(header: &[u8], hosts: &BTreeSet<String>) -> Result<Request, u16> {
    let text = std::str::from_utf8(header).map_err(|_| 400_u16)?;
    let mut lines = text.split("\r\n");
    let first = lines.next().ok_or(400_u16)?;
    let parts = first.split(' ').collect::<Vec<_>>();
    if parts.len() != 3 || parts[2] != "HTTP/1.1" {
        return Err(400);
    }
    let connect = parts[0] == "CONNECT";
    if !connect && !matches!(parts[0], "GET" | "HEAD") {
        return Err(405);
    }
    let (host, port, path) = if connect {
        let (host, port) = authority(parts[1])?;
        (host, port, String::new())
    } else {
        absolute_http(parts[1])?
    };
    if !hosts.contains(&host) || !matches!(port, 80 | 443) {
        return Err(403);
    }
    let mut forwarded = if connect {
        Vec::new()
    } else {
        format!("{} {} HTTP/1.1\r\n", parts[0], path).into_bytes()
    };
    let mut parsed_headers = Vec::new();
    let mut nominated_hop_headers = BTreeSet::new();
    for line in lines {
        if line.is_empty() {
            break;
        }
        let (name, value) = line.split_once(':').ok_or(400_u16)?;
        let lower = name.to_ascii_lowercase();
        if !name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
            || value.bytes().any(|byte| byte < b' ' && byte != b'\t')
        {
            return Err(400);
        }
        if lower == "proxy-authorization" {
            return Err(403);
        }
        if lower == "transfer-encoding" {
            return Err(413);
        }
        if lower == "connection" {
            for token in value.split(',').map(str::trim) {
                if token.is_empty()
                    || !token
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                {
                    return Err(400);
                }
                nominated_hop_headers.insert(token.to_ascii_lowercase());
            }
        }
        if lower == "content-length" && value.trim() != "0" {
            return Err(413);
        }
        parsed_headers.push((name, value, lower));
    }
    let mut saw_host = false;
    for (name, value, lower) in parsed_headers {
        if nominated_hop_headers.contains(&lower)
            || matches!(
                lower.as_str(),
                "connection"
                    | "proxy-connection"
                    | "keep-alive"
                    | "te"
                    | "trailer"
                    | "upgrade"
                    | "proxy-authenticate"
                    | "forwarded"
                    | "x-forwarded-for"
                    | "x-forwarded-host"
                    | "x-forwarded-proto"
            )
        {
            continue;
        }
        if lower == "host" {
            let (header_host, header_port) = authority_with_default(value.trim(), port)?;
            if header_host != host || header_port != port {
                return Err(400);
            }
            saw_host = true;
        }
        if !connect {
            forwarded.extend_from_slice(name.as_bytes());
            forwarded.extend_from_slice(b":");
            forwarded.extend_from_slice(value.as_bytes());
            forwarded.extend_from_slice(b"\r\n");
        }
    }
    if !connect && !saw_host {
        return Err(400);
    }
    if !connect {
        forwarded.extend_from_slice(b"Connection: close\r\n\r\n");
    }
    Ok(Request {
        host,
        port,
        connect,
        forward: forwarded,
    })
}

fn absolute_http(value: &str) -> Result<(String, u16, String), u16> {
    let rest = value.strip_prefix("http://").ok_or(403_u16)?;
    if rest.contains('@') || rest.contains('#') {
        return Err(400);
    }
    let split = rest.find('/').unwrap_or(rest.len());
    let (host, port) = authority_with_default(&rest[..split], 80)?;
    let path = if split == rest.len() {
        "/"
    } else {
        &rest[split..]
    };
    if path.len() > 8192 {
        return Err(414);
    }
    Ok((host, port, path.to_owned()))
}

fn authority(value: &str) -> Result<(String, u16), u16> {
    let (host, port) = value.rsplit_once(':').ok_or(400_u16)?;
    let port = port.parse::<u16>().map_err(|_| 400_u16)?;
    authority_checked(host, port)
}

fn authority_with_default(value: &str, default: u16) -> Result<(String, u16), u16> {
    if let Some((host, port)) = value.rsplit_once(':')
        && !host.contains(':')
    {
        return authority_checked(host, port.parse::<u16>().map_err(|_| 400_u16)?);
    }
    authority_checked(value, default)
}

fn authority_checked(host: &str, port: u16) -> Result<(String, u16), u16> {
    let host = host.to_ascii_lowercase();
    if !valid_hostname(&host) || blocked_metadata_name(&host) {
        return Err(403);
    }
    Ok((host, port))
}

fn valid_hostname(value: &str) -> bool {
    if value.len() > 253
        || value.is_empty()
        || value.parse::<IpAddr>().is_ok()
        || value.ends_with('.')
    {
        return false;
    }
    value.split('.').all(|label| {
        !label.is_empty()
            && label.len() <= 63
            && !label.starts_with('-')
            && !label.ends_with('-')
            && label
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
    }) && value.contains('.')
}

fn blocked_metadata_name(value: &str) -> bool {
    value == "localhost"
        || value == "metadata.google.internal"
        || value == "metadata.aws.internal"
        || value.ends_with(".localhost")
        || value.starts_with("metadata.")
}

fn resolve_public(host: &str, port: u16) -> Result<Vec<SocketAddr>, u16> {
    let mut command = Command::new(std::env::current_exe().map_err(|_| 502_u16)?);
    command.args(["--resolve", host, &port.to_string()]);
    let output =
        resolver_output(&mut command, Instant::now() + CONNECT_TIMEOUT).map_err(|_| 502_u16)?;
    let text = std::str::from_utf8(&output).map_err(|_| 502_u16)?;
    let mut addresses = BTreeSet::new();
    for line in text.lines() {
        addresses.insert(line.parse::<SocketAddr>().map_err(|_| 502_u16)?);
        if addresses.len() > MAX_RESOLVED_ADDRESSES {
            return Err(502);
        }
    }
    validate_resolved(addresses).map_err(|_| 403)
}

fn resolver_output(command: &mut Command, deadline: Instant) -> Result<Vec<u8>, ()> {
    let (mut output_stream, child_output) = UnixStream::pair().map_err(|_| ())?;
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::from(OwnedFd::from(child_output)))
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| ())?;
    // Command retains its configured descriptors after spawning. Drop its
    // writer so EOF really means that every child writer has closed.
    command.stdout(Stdio::null());
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                if !status.success() {
                    return Err(());
                }
                return read_resolver_output(&mut output_stream, deadline);
            }
            Ok(None) if Instant::now() < deadline => {
                thread::sleep(
                    Duration::from_millis(10)
                        .min(deadline.saturating_duration_since(Instant::now())),
                );
            }
            _ => {
                let _ = child.kill();
                let reap_deadline = Instant::now() + Duration::from_secs(1);
                while Instant::now() < reap_deadline {
                    match child.try_wait() {
                        Ok(None) => thread::sleep(Duration::from_millis(10)),
                        _ => break,
                    }
                }
                return Err(());
            }
        }
    }
}

fn read_resolver_output(stream: &mut UnixStream, deadline: Instant) -> Result<Vec<u8>, ()> {
    let mut output = Vec::new();
    let mut buffer = [0_u8; 1024];
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(());
        }
        stream.set_read_timeout(Some(remaining)).map_err(|_| ())?;
        let count = stream.read(&mut buffer).map_err(|_| ())?;
        if count == 0 {
            return Ok(output);
        }
        if output.len() + count > 4096 {
            return Err(());
        }
        output.extend_from_slice(&buffer[..count]);
    }
}

fn probe(mut stream: TcpStream, deadline: Instant) -> io::Result<()> {
    let mut request = &b"GET http://proxy.invalid/ HTTP/1.1\r\nHost: proxy.invalid\r\n\r\n"[..];
    while !request.is_empty() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(io::ErrorKind::TimedOut.into());
        }
        stream.set_write_timeout(Some(remaining))?;
        let written = stream.write(request)?;
        if written == 0 {
            return Err(io::ErrorKind::WriteZero.into());
        }
        request = &request[written..];
    }
    let mut response = [0_u8; 12];
    let mut received = 0;
    while received < response.len() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(io::ErrorKind::TimedOut.into());
        }
        stream.set_read_timeout(Some(remaining))?;
        let count = stream.read(&mut response[received..])?;
        if count == 0 {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        received += count;
    }
    if !response.starts_with(b"HTTP/1.1 403") {
        return Err(io::ErrorKind::InvalidData.into());
    }
    Ok(())
}

fn validate_resolved(addresses: BTreeSet<SocketAddr>) -> Result<Vec<SocketAddr>, ()> {
    if addresses.is_empty()
        || addresses.len() > MAX_RESOLVED_ADDRESSES
        || addresses.iter().any(|item| !public_ip(item.ip()))
    {
        return Err(());
    }
    Ok(addresses.into_iter().collect())
}

fn public_ip(value: IpAddr) -> bool {
    match value {
        IpAddr::V4(ip) => public_v4(ip),
        IpAddr::V6(ip) => public_v6(ip),
    }
}

fn public_v4(ip: Ipv4Addr) -> bool {
    let [a, b, c, _] = ip.octets();
    !(a == 0
        || a == 10
        || a == 127
        || a >= 224
        || (a == 100 && (64..=127).contains(&b))
        || (a == 169 && b == 254)
        || (a == 172 && (16..=31).contains(&b))
        || (a == 192 && b == 168)
        || (a == 192 && b == 0 && c == 0)
        || (a == 192 && b == 0 && c == 2)
        || (a == 192 && b == 88 && c == 99)
        || (a == 198 && (b == 18 || b == 19))
        || (a == 198 && b == 51 && c == 100)
        || (a == 203 && b == 0 && c == 113))
}

fn public_v6(ip: Ipv6Addr) -> bool {
    if let Some(v4) = ip.to_ipv4_mapped() {
        return public_v4(v4);
    }
    let octets = ip.octets();
    let first = ip.segments()[0];
    (first & 0xe000) == 0x2000
        && !ipv6_prefix(&octets, &[0x00, 0x64, 0xff, 0x9b], 96)
        && !ipv6_prefix(&octets, &[0x00, 0x64, 0xff, 0x9b, 0x00, 0x01], 48)
        && !ipv6_prefix(&octets, &[0x01, 0x00], 64)
        && !ipv6_prefix(&octets, &[0x20, 0x01], 23)
        && !ipv6_prefix(&octets, &[0x20, 0x01, 0x0d, 0xb8], 32)
        && !ipv6_prefix(&octets, &[0x20, 0x02], 16)
        && !ipv6_prefix(&octets, &[0x3f, 0xfe], 16)
        && !ipv6_prefix(&octets, &[0x3f, 0xff], 20)
}

fn ipv6_prefix(address: &[u8; 16], prefix: &[u8], bits: usize) -> bool {
    let full_bytes = bits / 8;
    let remaining_bits = bits % 8;
    for (index, byte) in address.iter().enumerate().take(full_bytes) {
        if *byte != prefix.get(index).copied().unwrap_or(0) {
            return false;
        }
    }
    remaining_bits == 0
        || address[full_bytes] >> (8 - remaining_bits)
            == prefix.get(full_bytes).copied().unwrap_or(0) >> (8 - remaining_bits)
}

fn connect_any(addresses: &[SocketAddr]) -> Result<TcpStream, ()> {
    let deadline = Instant::now() + CONNECT_TIMEOUT;
    for address in addresses {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        if let Ok(stream) =
            TcpStream::connect_timeout(address, remaining.min(Duration::from_secs(3)))
        {
            return Ok(stream);
        }
    }
    Err(())
}

fn read_header(stream: &mut TcpStream) -> io::Result<Vec<u8>> {
    let mut result = Vec::new();
    let mut byte = [0_u8; 1];
    while result.len() < MAX_HEADER_BYTES {
        if stream.read(&mut byte)? == 0 {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        result.push(byte[0]);
        if result.ends_with(b"\r\n\r\n") {
            return Ok(result);
        }
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidData,
        "header exceeds limit",
    ))
}

fn tunnel(mut left: TcpStream, mut right: TcpStream) -> io::Result<()> {
    let mut left_read = left.try_clone()?;
    let mut right_write = right.try_clone()?;
    let budget = Arc::new(AtomicU64::new(0));
    let outbound_budget = Arc::clone(&budget);
    let outbound =
        thread::spawn(move || copy_bounded(&mut left_read, &mut right_write, &outbound_budget));
    let inbound = copy_bounded(&mut right, &mut left, &budget);
    let outbound = outbound
        .join()
        .unwrap_or_else(|_| Err(io::ErrorKind::Other.into()));
    inbound.and(outbound).map(|_| ())
}

fn copy_bounded(
    reader: &mut TcpStream,
    writer: &mut TcpStream,
    budget: &AtomicU64,
) -> io::Result<u64> {
    let started = Instant::now();
    let mut copied = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        if started.elapsed() > Duration::from_secs(7200) {
            return Err(io::ErrorKind::TimedOut.into());
        }
        let read = reader.read(&mut buffer)?;
        if read == 0 {
            return Ok(copied);
        }
        copied = copied
            .checked_add(read as u64)
            .ok_or(io::ErrorKind::FileTooLarge)?;
        // fetch_add is an atomic read-modify-write, so the two directions
        // cannot both pass this check and exceed the shared budget.
        let total = budget
            .fetch_add(read as u64, Ordering::Relaxed)
            .checked_add(read as u64)
            .ok_or(io::ErrorKind::FileTooLarge)?;
        if total > MAX_TUNNEL_BYTES {
            return Err(io::ErrorKind::FileTooLarge.into());
        }
        writer.write_all(&buffer[..read])?;
    }
}

fn reject(mut stream: TcpStream, status: u16) {
    if stream
        .set_write_timeout(Some(Duration::from_secs(2)))
        .is_err()
    {
        return;
    }
    let reason = match status {
        400 => "Bad Request",
        403 => "Forbidden",
        405 => "Method Not Allowed",
        413 => "Payload Too Large",
        414 => "URI Too Long",
        502 => "Bad Gateway",
        _ => "Service Unavailable",
    };
    let _ = write!(
        stream,
        "HTTP/1.1 {status} {reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
    );
}

#[cfg(test)]
mod tests {
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
        let started = Instant::now();
        assert!(
            resolver_output(
                Command::new("/bin/sleep").arg("10"),
                started + Duration::from_millis(50)
            )
            .is_err()
        );
        assert!(started.elapsed() < Duration::from_secs(1));
        assert_eq!(
            resolver_output(
                Command::new("/bin/echo").arg("1.1.1.1:443"),
                Instant::now() + Duration::from_secs(1)
            )
            .unwrap(),
            b"1.1.1.1:443\n"
        );
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
}
