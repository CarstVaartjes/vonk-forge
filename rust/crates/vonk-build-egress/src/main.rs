use std::{
    collections::BTreeSet,
    io::{self, Read, Write},
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, TcpListener, TcpStream, ToSocketAddrs},
    os::{fd::OwnedFd, unix::net::UnixStream},
    process::{Command, ExitCode, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

use vonk_build_egress::request::parse_request;

const LISTEN: &str = "0.0.0.0:18080";
const MAX_HEADER_BYTES: usize = 16 * 1024;
const MAX_CONNECTIONS: usize = 64;
const MAX_RESOLVED_ADDRESSES: usize = 16;
const MAX_TUNNEL_BYTES: u64 = 16 * 1024 * 1024 * 1024;
/// Byte budget for one connection, shared across both copy directions so a
/// tunnel cannot move `MAX_TUNNEL_BYTES` in each direction.
const IO_TIMEOUT: Duration = Duration::from_secs(120);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(15);
static RESOLVER_OWNERS: AtomicUsize = AtomicUsize::new(0);

fn main() -> ExitCode {
    match run(std::env::args().skip(1).collect()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("vonk-build-egress: {error}");
            ExitCode::from(2)
        }
    }
}

#[expect(
    clippy::needless_pass_by_value,
    reason = "the call takes ownership at this boundary; the caller's value is consumed"
)]
fn run(arguments: Vec<String>) -> Result<(), String> {
    if arguments == ["--probe"] {
        probe_available(
            SocketAddr::from(([127, 0, 0, 1], 18080)),
            Instant::now() + Duration::from_secs(2),
        )
        .map_err(|_| "proxy probe unavailable")?;
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
        // The launch arguments carry the accepted host set. Request ingress
        // still checks hostname syntax and metadata/private destinations.
        hosts.insert(host);
        index += 2;
    }
    let hosts = Arc::new(hosts);
    let mut retry_seconds = 1_u64;
    loop {
        // A bind/listener fault is local state, not a refusal of build authority.
        // Each attempt releases its listener before the next bounded delay.
        match serve(Arc::clone(&hosts)) {
            Ok(()) => return Ok(()),
            Err(error) => {
                eprintln!(
                    "vonk-build-egress: listener unavailable ({error}); next observation in {retry_seconds} seconds"
                );
                let retry_deadline = Instant::now() + Duration::from_secs(retry_seconds);
                thread::sleep(retry_deadline.saturating_duration_since(Instant::now()));
                retry_seconds = retry_seconds.saturating_mul(2).min(30);
            }
        }
    }
}

#[expect(
    clippy::needless_pass_by_value,
    reason = "the call takes ownership at this boundary; the caller's value is consumed"
)]
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
        // The guard owns the slot from here on: dropping it, in the worker or
        // with a closure that never started, releases the slot exactly once.
        let guard = ConnectionGuard(Arc::clone(&active));
        let hosts = Arc::clone(&hosts);
        let refusal = stream.try_clone();
        let spawned = thread::Builder::new().spawn(move || {
            let _guard = guard;
            let _ = handle(stream, &hosts);
        });
        if let Err(error) = spawned {
            // The host cannot give this connection a thread right now: say so
            // and keep serving; the client retries.
            eprintln!("vonk-build-egress: no thread for a connection ({error})");
            if let Ok(refusal) = refusal {
                reject(refusal, 503);
            }
        }
    }
    Ok(())
}

struct ConnectionGuard(Arc<AtomicUsize>);
impl Drop for ConnectionGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}

fn handle(client: TcpStream, hosts: &BTreeSet<String>) -> io::Result<()> {
    handle_until(
        client,
        hosts,
        Instant::now() + Duration::from_secs(7200),
        |host, port, deadline| {
            resolve_public_until(host, port, deadline.min(Instant::now() + CONNECT_TIMEOUT))
        },
        |addresses, deadline| {
            connect_until(addresses, deadline.min(Instant::now() + CONNECT_TIMEOUT))
        },
    )
}

#[cfg(test)]
fn handle_observed(
    client: TcpStream,
    hosts: &BTreeSet<String>,
    mut resolve: impl FnMut(&str, u16) -> Result<Vec<SocketAddr>, u16>,
    mut connect: impl FnMut(&[SocketAddr]) -> Result<TcpStream, ()>,
) -> io::Result<()> {
    handle_until(
        client,
        hosts,
        Instant::now() + Duration::from_secs(7200),
        |host, port, _| resolve(host, port),
        |addresses, _| connect(addresses),
    )
}

fn handle_until(
    mut client: TcpStream,
    hosts: &BTreeSet<String>,
    deadline: Instant,
    mut resolve: impl FnMut(&str, u16, Instant) -> Result<Vec<SocketAddr>, u16>,
    mut connect: impl FnMut(&[SocketAddr], Instant) -> Result<TcpStream, ()>,
) -> io::Result<()> {
    client.set_read_timeout(Some(IO_TIMEOUT))?;
    client.set_write_timeout(Some(IO_TIMEOUT))?;
    let header = read_header_until(&mut client, deadline.min(Instant::now() + IO_TIMEOUT))?;
    let request = match parse_request(&header, hosts) {
        Ok(request) => request,
        Err(status) => {
            reject(client, status);
            return Ok(());
        }
    };
    let addresses = match resolve(&request.host, request.port, deadline) {
        Ok(value) => value,
        Err(status) => {
            reject(client, status);
            return Ok(());
        }
    };
    let Ok(mut upstream) = connect(&addresses, deadline) else {
        reject(client, 502);
        return Ok(());
    };
    upstream.set_read_timeout(Some(IO_TIMEOUT))?;
    upstream.set_write_timeout(Some(IO_TIMEOUT))?;
    if request.connect {
        write_until(
            &mut client,
            b"HTTP/1.1 200 Connection Established\r\n\r\n",
            deadline,
        )?;
        tunnel_until(client, upstream, deadline)
    } else {
        write_until(&mut upstream, &request.forward, deadline)?;
        copy_until(&mut upstream, &mut client, &AtomicU64::new(0), deadline).map(|_| ())
    }
}

fn resolve_public_until(host: &str, port: u16, deadline: Instant) -> Result<Vec<SocketAddr>, u16> {
    resolve_observed(port, deadline, |deadline| {
        let mut command = Command::new(std::env::current_exe().map_err(|_| ())?);
        command.args(["--resolve", host, &port.to_string()]);
        resolver_output(&mut command, deadline)
    })
}

// All observations for a request share one budget. Empty, malformed, failed
// and oversized replies are unknown; only a proved denied address is 403.
fn resolve_observed(
    port: u16,
    deadline: Instant,
    mut observe: impl FnMut(Instant) -> Result<Vec<u8>, ()>,
) -> Result<Vec<SocketAddr>, u16> {
    for attempt in 0..3 {
        if Instant::now() >= deadline {
            break;
        }
        let attempt_deadline = deadline.min(Instant::now() + CONNECT_TIMEOUT / 3);
        if let Ok(output) = observe(attempt_deadline)
            && let Ok(text) = std::str::from_utf8(&output)
        {
            let mut addresses = BTreeSet::new();
            let mut valid = true;
            for line in text.lines() {
                match line.parse::<SocketAddr>() {
                    Ok(address) if address.port() == port => {
                        // Do not allow malformed sibling data to hide a proved
                        // private destination.
                        if !public_ip(address.ip()) {
                            return Err(403);
                        }
                        addresses.insert(address);
                    }
                    _ => valid = false,
                }
                if addresses.len() > MAX_RESOLVED_ADDRESSES {
                    valid = false;
                    break;
                }
            }
            if valid && !addresses.is_empty() {
                return validate_resolved(addresses).map_err(|()| 502);
            }
        }
        if attempt < 2 {
            thread::sleep(
                Duration::from_millis(50 * (attempt + 1))
                    .min(deadline.saturating_duration_since(Instant::now())),
            );
        }
    }
    Err(502)
}

// The owner follows the child, including after the request deadline. Admission
// remains bounded when an OS cannot terminate a child immediately.
struct ResolverOwner(&'static AtomicUsize);
impl ResolverOwner {
    fn acquire() -> Result<Self, ()> {
        Self::acquire_at(&RESOLVER_OWNERS)
    }

    fn acquire_at(counter: &'static AtomicUsize) -> Result<Self, ()> {
        counter
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |count| {
                (count < MAX_CONNECTIONS).then_some(count + 1)
            })
            .map(|_| Self(counter))
            .map_err(|_| ())
    }
}
impl Drop for ResolverOwner {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}

fn resolver_output(command: &mut Command, deadline: Instant) -> Result<Vec<u8>, ()> {
    supervise_retained_resolvers();
    let owner = ResolverOwner::acquire()?;
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
                // Transfer the exact child and its resource claim to a reaper.
                // A failed kill never drops a live child or releases its slot.
                let _ = child.kill();
                retain_resolver(child, owner, |child| {
                    let _ = child.kill();
                });
                return Err(());
            }
        }
    }
}

// Each failed request has a bounded termination phase. After that phase an
// exact child/claim pair belongs to the service supervisor, never to a waiting
// request thread. Fresh admission drives a bounded supervision pass even when
// every slot is retained; claims are released only after confirmed reap.
static RETAINED_RESOLVERS: Mutex<Vec<(std::process::Child, ResolverOwner)>> =
    Mutex::new(Vec::new());
const RESOLVER_REAP_BUDGET: Duration = Duration::from_secs(1);

fn retain_resolver(
    child: std::process::Child,
    owner: ResolverOwner,
    terminate: impl FnMut(&mut std::process::Child) + Send + 'static,
) {
    retain_resolver_until(
        child,
        owner,
        terminate,
        Instant::now() + RESOLVER_REAP_BUDGET,
    );
}

fn retain_resolver_until(
    child: std::process::Child,
    owner: ResolverOwner,
    terminate: impl FnMut(&mut std::process::Child) + Send + 'static,
    deadline: Instant,
) {
    retain_resolver_with(child, owner, terminate, deadline, |work| {
        thread::Builder::new().spawn(work).map(|_| ())
    });
}

/// `start` runs the reaper; a host that cannot start it hands the pair to
/// service supervision at once.
fn retain_resolver_with(
    child: std::process::Child,
    owner: ResolverOwner,
    mut terminate: impl FnMut(&mut std::process::Child) + Send + 'static,
    deadline: Instant,
    start: impl FnOnce(Box<dyn FnOnce() + Send>) -> io::Result<()>,
) {
    // The pair lives in a shared slot so a reaper thread that cannot start
    // never takes the child or its claim down with the unstarted closure.
    let slot = Arc::new(Mutex::new(Some((child, owner))));
    let reaper_slot = Arc::clone(&slot);
    let spawned = start(Box::new(move || {
        let Some((mut child, owner)) = reaper_slot
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take()
        else {
            return;
        };
        while Instant::now() < deadline {
            if matches!(child.try_wait(), Ok(Some(_))) {
                return;
            }
            terminate(&mut child);
            thread::sleep(
                Duration::from_millis(10).min(deadline.saturating_duration_since(Instant::now())),
            );
        }
        // No child or claim is dropped on budget expiry. The service takes
        // ownership in one short critical section, with no external wait.
        RETAINED_RESOLVERS
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push((child, owner));
    }));
    if spawned.is_err()
        && let Some(pair) = slot
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take()
    {
        // Service supervision takes over at once; no claim is dropped.
        RETAINED_RESOLVERS
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push(pair);
    }
}

fn supervise_retained_resolvers() {
    let Ok(mut retained) = RETAINED_RESOLVERS.try_lock() else {
        return;
    };
    // Actual admission bounds this collection to MAX_CONNECTIONS children.
    retained.retain_mut(|(child, _owner)| {
        if matches!(child.try_wait(), Ok(Some(_))) {
            false
        } else {
            let _ = child.kill();
            true
        }
    });
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

fn probe_available(address: SocketAddr, deadline: Instant) -> io::Result<()> {
    for attempt in 0..3 {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        let attempt_deadline = deadline.min(Instant::now() + Duration::from_millis(500));
        if let Ok(stream) = TcpStream::connect_timeout(
            &address,
            attempt_deadline.saturating_duration_since(Instant::now()),
        ) && probe(stream, attempt_deadline).is_ok()
        {
            return Ok(());
        }
        if attempt < 2 {
            thread::sleep(
                Duration::from_millis(50 * (attempt + 1))
                    .min(deadline.saturating_duration_since(Instant::now())),
            );
        }
    }
    Err(io::ErrorKind::TimedOut.into())
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

fn connect_until(addresses: &[SocketAddr], deadline: Instant) -> Result<TcpStream, ()> {
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

#[cfg(test)]
fn read_header(stream: &mut TcpStream) -> io::Result<Vec<u8>> {
    read_header_until(stream, Instant::now() + IO_TIMEOUT)
}

fn read_header_until(stream: &mut TcpStream, deadline: Instant) -> io::Result<Vec<u8>> {
    let mut result = Vec::new();
    let mut byte = [0_u8; 1];
    while result.len() < MAX_HEADER_BYTES {
        stream.set_read_timeout(Some(remaining_io(deadline)?))?;
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

fn tunnel_until(mut left: TcpStream, mut right: TcpStream, deadline: Instant) -> io::Result<()> {
    let mut left_read = left.try_clone()?;
    let mut right_write = right.try_clone()?;
    let budget = Arc::new(AtomicU64::new(0));
    let outbound_budget = Arc::clone(&budget);
    // No thread for the second direction: fail this tunnel and close both
    // streams rather than take the whole proxy down.
    let outbound = thread::Builder::new().spawn(move || {
        let copied = copy_until(&mut left_read, &mut right_write, &outbound_budget, deadline);
        // The client is gone (or failed): send the upstream our FIN so it
        // closes and the other direction ends now, not after its 120 s read
        // timeout, and the connection slot is released.
        let _ = right_write.shutdown(std::net::Shutdown::Write);
        copied
    })?;
    let inbound = copy_until(&mut right, &mut left, &budget, deadline);
    let _ = left.shutdown(std::net::Shutdown::Both);
    let _ = right.shutdown(std::net::Shutdown::Both);
    let outbound = outbound
        .join()
        .unwrap_or_else(|_| Err(io::ErrorKind::Other.into()));
    inbound.and(outbound).map(|_| ())
}

fn copy_until(
    reader: &mut TcpStream,
    writer: &mut TcpStream,
    budget: &AtomicU64,
    deadline: Instant,
) -> io::Result<u64> {
    let mut copied = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        reader.set_read_timeout(Some(remaining_io(deadline)?))?;
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
        let mut pending = &buffer[..read];
        while !pending.is_empty() {
            writer.set_write_timeout(Some(remaining_io(deadline)?))?;
            let written = writer.write(pending)?;
            if written == 0 {
                return Err(io::ErrorKind::WriteZero.into());
            }
            pending = &pending[written..];
        }
    }
}

fn write_until(stream: &mut TcpStream, mut bytes: &[u8], deadline: Instant) -> io::Result<()> {
    while !bytes.is_empty() {
        stream.set_write_timeout(Some(remaining_io(deadline)?))?;
        let written = stream.write(bytes)?;
        if written == 0 {
            return Err(io::ErrorKind::WriteZero.into());
        }
        bytes = &bytes[written..];
    }
    Ok(())
}

fn remaining_io(deadline: Instant) -> io::Result<Duration> {
    let remaining = deadline
        .saturating_duration_since(Instant::now())
        .min(IO_TIMEOUT);
    if remaining.is_zero() {
        Err(io::ErrorKind::TimedOut.into())
    } else {
        Ok(remaining)
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
mod tests;
