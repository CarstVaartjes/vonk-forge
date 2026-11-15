//! Ask the root helper which runtime images Docker holds: the unprivileged
//! agent has no Docker access.
use std::{path::Path, time::Duration};
use vonk_agent_protocol::{
    canonical_generated_json,
    generated::{
        RuntimeImageInspectionKind, RuntimeImageInspectionRequest, RuntimeImageObservation,
    },
};

/// `None` (unknown) when the helper cannot answer; the next inventory asks again.
pub(crate) fn observe(socket: &Path) -> Option<RuntimeImageObservation> {
    let request = RuntimeImageInspectionRequest {
        kind: RuntimeImageInspectionKind::RuntimeImages,
    };
    let body = canonical_generated_json(&request).ok()?;
    let reply = crate::stall_window::Inactivity::new(Duration::from_secs(20));
    crate::host_runtime::inspect_helper(socket, &body, &reply).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::{Read, Write},
        os::unix::net::UnixListener,
    };

    const VECTOR: &str = include_str!(
        "../../../../agent_protocol/src/vonk_agent_protocol/vectors/runtime-images-v1.json"
    );

    #[test]
    fn the_helper_listing_is_reported_and_an_unavailable_helper_is_unknown() {
        // Break caught: a helper failure read as "holds nothing" (which would
        // look like a prune) or one failure sticking to later inventories.
        let root = tempfile::tempdir().unwrap();
        let socket = root.path().join("helper.sock");
        assert!(observe(&socket).is_none());
        let listener = UnixListener::bind(&socket).unwrap();
        let (done, completed) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let held: RuntimeImageObservation = serde_json::from_str(VECTOR).unwrap();
            for body in [
                br#"{"images":"bad"}"#.to_vec(),
                canonical_generated_json(&held).unwrap(),
            ] {
                let (mut stream, _) = listener.accept().unwrap();
                let mut prefix = [0; 4];
                stream.read_exact(&mut prefix).unwrap();
                let mut request = vec![0; u32::from_be_bytes(prefix) as usize];
                stream.read_exact(&mut request).unwrap();
                let request: RuntimeImageInspectionRequest =
                    serde_json::from_slice(&request).unwrap();
                assert_eq!(request.kind, RuntimeImageInspectionKind::RuntimeImages);
                stream
                    .write_all(&(body.len() as u32).to_be_bytes())
                    .unwrap();
                stream.write_all(&body).unwrap();
            }
            done.send(()).unwrap();
        });
        assert!(observe(&socket).is_none());
        let observed = observe(&socket).unwrap();
        assert_eq!(
            serde_json::to_value(&observed).unwrap(),
            serde_json::from_str::<serde_json::Value>(VECTOR).unwrap()
        );
        completed.recv_timeout(Duration::from_secs(5)).unwrap();
    }
}
