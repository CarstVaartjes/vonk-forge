use super::*;

pub(super) fn write_request(
    root: &Path,
    digest: &str,
    body: &[u8],
) -> Result<PathBuf, HostRuntimeError> {
    // Publish the directory with its owner-only mode in the mkdir itself.
    // Parallel callers must never observe an intermediate permissive root.
    match fs::DirBuilder::new().mode(0o700).create(root) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error.into()),
    }
    let metadata = fs::symlink_metadata(root)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        // Keep the exact unsafe object without following it. This generated
        // request projection is reconstructed from current signed bytes.
        fs::rename(
            root,
            root.with_extension(format!("unavailable-{}", uuid::Uuid::new_v4())),
        )?;
        fs::DirBuilder::new().mode(0o700).create(root)?;
    } else if metadata.permissions().mode() & 0o077 != 0 {
        fs::set_permissions(root, fs::Permissions::from_mode(0o700))?;
    }
    let destination = root.join(format!("{digest}.json"));
    match fs::symlink_metadata(&destination) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink()
                || !metadata.is_file()
                || metadata.nlink() != 1
                || metadata.permissions().mode() & 0o077 != 0
                || !stored_request_matches(&destination, body)
            {
                let isolated = root.join(format!(".{digest}.unavailable-{}", uuid::Uuid::new_v4()));
                fs::rename(&destination, isolated)?;
                File::open(root)?.sync_all()?;
            } else {
                return Ok(destination);
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    let nonce = uuid::Uuid::new_v4();
    let temporary = root.join(format!(".{digest}.{}.{nonce}.tmp", std::process::id()));
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(body)?;
    file.sync_all()?;
    match fs::hard_link(&temporary, &destination) {
        Ok(()) => fs::remove_file(&temporary)?,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            fs::remove_file(&temporary)?;
            let metadata = fs::symlink_metadata(&destination)?;
            if metadata.file_type().is_symlink()
                || !metadata.is_file()
                || metadata.nlink() != 1
                || metadata.permissions().mode() & 0o077 != 0
                || !stored_request_matches(&destination, body)
            {
                return Err(HostRuntimeError::HelperProtocol(
                    HelperProtocolCause::RequestStorage,
                ));
            }
            return Ok(destination);
        }
        Err(error) => {
            let _ = fs::remove_file(&temporary);
            return Err(error.into());
        }
    }
    File::open(root)?.sync_all()?;
    Ok(destination)
}

fn stored_request_matches(path: &Path, body: &[u8]) -> bool {
    let Ok(file) = OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)
    else {
        return false;
    };
    let Ok(metadata) = file.metadata() else {
        return false;
    };
    if !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.len() != body.len() as u64
        || metadata.permissions().mode() & 0o077 != 0
    {
        return false;
    }
    let mut observed = Vec::new();
    file.take(body.len() as u64 + 1)
        .read_to_end(&mut observed)
        .is_ok()
        && observed == body
}

/// Exchange one framed request with the helper. The helper answers once, at
/// the end; the wait for that answer ends when `reply` sees no progress for a
/// whole window. Progress is any reply byte, or what the caller observes on
/// the helper's behalf (the bytes an image pull reads through the agent).
pub(super) fn call_helper(
    socket: &Path,
    body: &[u8],
    reply: &Inactivity,
) -> Result<HelperResponse, HostRuntimeError> {
    if body.is_empty() || body.len() > MAX_HELPER_MESSAGE_BYTES {
        // Report the ceiling and the observed length; the body itself is
        // engine-owned content and never travels into the evidence.
        return Err(HostRuntimeError::HelperProtocolBound {
            cause: HelperProtocolCause::MessageFraming,
            limit: Some(MAX_HELPER_MESSAGE_BYTES as u64),
            observed: body.len() as u64,
        });
    }
    let mut stream = UnixStream::connect(socket)?;
    stream.set_write_timeout(Some(Duration::from_secs(10)))?;
    stream.write_all(&(body.len() as u32).to_be_bytes())?;
    stream.write_all(body)?;
    stream.flush()?;
    let mut prefix = [0_u8; 4];
    read_observed(&mut stream, &mut prefix, reply)?;
    let length = u32::from_be_bytes(prefix) as usize;
    if length == 0 || length > MAX_HELPER_MESSAGE_BYTES {
        return Err(HostRuntimeError::HelperProtocol(
            HelperProtocolCause::MessageFraming,
        ));
    }
    let mut response = vec![0_u8; length];
    read_observed(&mut stream, &mut response, reply)?;
    parse_strict(&response)
        .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::MessageFraming))
}

/// Read exactly `bytes` while `reply` keeps seeing progress.
pub(crate) fn read_observed(
    stream: &mut UnixStream,
    bytes: &mut [u8],
    reply: &Inactivity,
) -> std::io::Result<()> {
    let mut received = 0;
    while received < bytes.len() {
        if reply.expired() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                reply.stalled(),
            ));
        }
        // Re-check the window at least every second: progress observed
        // elsewhere moves its deadline while this read blocks.
        let remaining = reply
            .deadline()
            .saturating_duration_since(tokio::time::Instant::now());
        stream.set_read_timeout(Some(
            remaining.clamp(Duration::from_millis(1), Duration::from_secs(1)),
        ))?;
        match stream.read(&mut bytes[received..]) {
            Ok(0) => return Err(std::io::ErrorKind::UnexpectedEof.into()),
            Ok(count) => {
                received += count;
                reply.observe();
            }
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}
