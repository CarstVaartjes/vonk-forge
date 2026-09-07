//! Disposable root-owned ARM64 systemd acceptance driver, never packaged.
use std::io::Read;
use std::path::Path;
use vonk_agent_helper::operations::{ManagedRoots, OperationExecutor, ProcessCommandRunner};
use vonk_agent_helper::protocol::HostOperation;
fn main() {
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).unwrap();
    let operation: HostOperation = vonk_agent_protocol::parse_strict(input.as_bytes()).unwrap();
    let key = std::fs::read("/usr/share/keyrings/vonk-forge-release.pub").unwrap();
    let key = hex::decode(String::from_utf8(key).unwrap().trim()).unwrap();
    let executor = OperationExecutor::new(
        ManagedRoots::under(Path::new("/var/lib/vonk-forge"))
            .with_package_custody(Path::new("/run/vonk-forge-package-candidates")),
        &key,
        ProcessCommandRunner,
        Some(0),
    )
    .unwrap()
    .with_package_owner(0);
    match executor.execute_for_node(&operation, Some("spk_11111111111111111111111111111111")) {
        Ok(outcome) => println!("{}", serde_json::to_string(&outcome).unwrap()),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1)
        }
    }
}
