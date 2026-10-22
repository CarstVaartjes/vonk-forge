//! Ask the existing root helper: unprivileged iomem hides physical addresses.
use std::{path::Path, time::Duration};
use vonk_agent_protocol::{
    canonical_generated_json,
    generated::{
        DisplayMemoryInspectionKind, DisplayMemoryInspectionRequest, DisplayMemoryObservation,
    },
};

pub(crate) fn observe(
    socket: &Path,
    memory_total_bytes: Option<u64>,
) -> Option<DisplayMemoryObservation> {
    let request = DisplayMemoryInspectionRequest {
        kind: DisplayMemoryInspectionKind::DisplayMemory,
    };
    let body = canonical_generated_json(&request).ok()?;
    let reply = crate::stall_window::Inactivity::new(Duration::from_secs(5));
    let mut observation: DisplayMemoryObservation =
        crate::host_runtime::inspect_helper(socket, &body, &reply).ok()?;
    observation.memory_total_bytes = memory_total_bytes;
    Some(observation)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::{Read, Write},
        os::unix::net::UnixListener,
    };

    #[test]
    fn helper_unavailable_or_invalid_is_unknown_and_the_next_inventory_recovers() {
        // Break caught: reading masked iomem as the unprivileged agent, retaining
        // stale classification on helper failure, or making one failure sticky.
        let root = tempfile::tempdir().unwrap();
        let socket = root.path().join("helper.sock");
        assert!(observe(&socket, Some(123)).is_none());
        let listener = UnixListener::bind(&socket).unwrap();
        let (done, completed) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let hosts: Vec<vonk_agent_protocol::generated::HostSettings> =
                serde_json::from_str(include_str!(
                    "../../../../agent_protocol/src/vonk_agent_protocol/vectors/display-memory.json"
                ))
                .unwrap();
            for body in [
                canonical_generated_json(hosts[1].display_memory.as_ref().unwrap()).unwrap(),
                br#"{"carveout_bytes":"bad"}"#.to_vec(),
                canonical_generated_json(hosts[0].display_memory.as_ref().unwrap()).unwrap(),
            ] {
                let (mut stream, _) = listener.accept().unwrap();
                let mut prefix = [0; 4];
                stream.read_exact(&mut prefix).unwrap();
                let mut request = vec![0; u32::from_be_bytes(prefix) as usize];
                stream.read_exact(&mut request).unwrap();
                let request: DisplayMemoryInspectionRequest =
                    serde_json::from_slice(&request).unwrap();
                assert_eq!(request.kind, DisplayMemoryInspectionKind::DisplayMemory);
                stream
                    .write_all(&(body.len() as u32).to_be_bytes())
                    .unwrap();
                stream.write_all(&body).unwrap();
            }
            done.send(()).unwrap();
        });
        let observed = observe(&socket, Some(123)).unwrap();
        assert_eq!(observed.carveout_bytes, Some(0x20000000));
        assert_eq!(observed.memory_total_bytes, Some(123));
        assert!(observe(&socket, Some(123)).is_none());
        assert_eq!(
            observe(&socket, Some(123)).unwrap().carveout_bytes,
            Some(0x80000000)
        );
        completed.recv_timeout(Duration::from_secs(5)).unwrap();
    }
}
