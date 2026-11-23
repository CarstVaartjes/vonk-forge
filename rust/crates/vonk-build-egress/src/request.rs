//! Request-line and header parsing for the build-egress proxy: decides, from the
//! raw header bytes alone, whether a request may be forwarded and what is sent.

use std::{collections::BTreeSet, net::IpAddr};

#[derive(Debug)]
pub struct Request {
    pub host: String,
    pub port: u16,
    pub connect: bool,
    pub forward: Vec<u8>,
}

pub fn parse_request(header: &[u8], hosts: &BTreeSet<String>) -> Result<Request, u16> {
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

#[expect(
    clippy::string_slice,
    reason = "the index comes from a search or prefix check on this same string, so it sits on a character boundary"
)]
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

#[cfg(test)]
mod tests {
    use super::*;

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
    fn blocked_metadata_names_are_refused() {
        assert!(blocked_metadata_name("metadata.google.internal"));
    }

    /// Invariant for any input: no panic, and an accepted request names an
    /// allowlisted host on port 80 or 443.
    #[test]
    #[expect(
        clippy::cast_possible_truncation,
        reason = "a pseudo-random byte takes the low 8 bits by intent"
    )]
    fn accepted_requests_stay_inside_the_allowlist() {
        let hosts = BTreeSet::from(["pypi.org".to_owned(), "files.pythonhosted.org".to_owned()]);
        let seeds: [&[u8]; 4] = [
            b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n",
            b"GET http://pypi.org/simple HTTP/1.1\r\nHost: pypi.org\r\nUser-Agent: t\r\n\r\n",
            b"CONNECT other.example:443 HTTP/1.1\r\n\r\n",
            b"GET http://pypi.org:8080/ HTTP/1.1\r\nHost: pypi.org:8080\r\n\r\n",
        ];
        let mut state = 0x9e37_79b9_u32;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            state
        };
        let mut accepted = 0;
        for round in 0..4000 {
            let mut bytes = seeds[round % seeds.len()].to_vec();
            for _ in 0..=(next() % 4) {
                let at = next() as usize % bytes.len();
                match next() % 3 {
                    0 => bytes[at] = next() as u8,
                    1 => {
                        bytes.remove(at);
                    }
                    _ => bytes.insert(at, b":@. /\r\n"[next() as usize % 7]),
                }
            }
            if let Ok(request) = parse_request(&bytes, &hosts) {
                accepted += 1;
                assert!(hosts.contains(&request.host), "{bytes:?}");
                assert!(matches!(request.port, 80 | 443), "{bytes:?}");
            }
        }
        assert!(accepted > 0);
    }
}
