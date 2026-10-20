use super::*;
use std::io::Cursor;

fn fixture() -> (Vec<u8>, RecipeBuildBaseImage, std::ops::Range<u64>) {
    let config = serde_json::to_vec(&ImageConfig {
        os: "linux".to_owned(),
        architecture: "arm64".to_owned(),
    })
    .unwrap();
    let layer = vec![42; 8192];
    let descriptor = |content: &[u8], media_type: &str| Descriptor {
        media_type: media_type.to_owned(),
        digest: format!("sha256:{}", hex_digest(content)),
        size: content.len() as u64,
        annotations: BTreeMap::new(),
    };
    let manifest = serde_json::to_vec(&Manifest {
        schema_version: 2,
        media_type: OCI_MANIFEST.to_owned(),
        config: descriptor(&config, OCI_CONFIG),
        layers: vec![descriptor(&layer, "application/vnd.oci.image.layer.v1.tar")],
    })
    .unwrap();
    let digest = format!("sha256:{}", hex_digest(&manifest));
    let index = serde_json::to_vec(&Index {
        schema_version: 2,
        manifests: vec![Descriptor {
            media_type: OCI_MANIFEST.to_owned(),
            digest: digest.clone(),
            size: manifest.len() as u64,
            annotations: BTreeMap::from([(
                "org.opencontainers.image.ref.name".to_owned(),
                "registry.example/original:tag".to_owned(),
            )]),
        }],
    })
    .unwrap();
    let mut bytes = Vec::new();
    let layer_path = blob_path(&format!("sha256:{}", hex_digest(&layer))).unwrap();
    {
        let mut archive = tar::Builder::new(&mut bytes);
        for (path, value) in [
            ("oci-layout".to_owned(), OCI_LAYOUT),
            ("index.json".to_owned(), index.as_slice()),
            (layer_path.clone(), layer.as_slice()),
            (blob_path(&digest).unwrap(), manifest.as_slice()),
            (
                blob_path(&format!("sha256:{}", hex_digest(&config))).unwrap(),
                config.as_slice(),
            ),
        ] {
            let mut header = tar::Header::new_ustar();
            header.set_path(path).unwrap();
            header.set_size(value.len() as u64);
            header.set_mode(0o644);
            header.set_cksum();
            archive.append(&header, Cursor::new(value)).unwrap();
        }
        archive.finish().unwrap();
    }
    let mut archive = tar::Archive::new(Cursor::new(&bytes));
    let entry = archive
        .entries()
        .unwrap()
        .map(Result::unwrap)
        .find(|entry| entry.path().unwrap() == Path::new(&layer_path))
        .unwrap();
    let layer_range = entry.raw_file_position()..entry.raw_file_position() + entry.size();
    (
        bytes,
        RecipeBuildBaseImage {
            reference: format!("registry.example/original:tag@{digest}"),
            manifest_digest: digest,
        },
        layer_range,
    )
}

// Fail on any layer read, including a read that does not feed a hasher.
struct UnreadableLayer {
    source: Cursor<Vec<u8>>,
    layer: std::ops::Range<u64>,
}
impl Read for UnreadableLayer {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        let start = self.source.position();
        let end = start.saturating_add(buffer.len() as u64);
        assert!(
            end <= self.layer.start || start >= self.layer.end,
            "warm reuse read layer bytes"
        );
        self.source.read(buffer)
    }
}
impl Seek for UnreadableLayer {
    fn seek(&mut self, position: SeekFrom) -> std::io::Result<u64> {
        self.source.seek(position)
    }
}

#[test]
fn warm_archive_reuse_never_reads_layer_bytes() {
    let (bytes, image, layer) = fixture();
    let source = UnreadableLayer {
        source: Cursor::new(bytes),
        layer,
    };
    let budget = TransferBudget {
        deadline: Instant::now() + Duration::from_secs(10),
        cancelled: &|| false,
    };
    inspect_archive(source, &image, "linux/arm64", &budget).unwrap();
}

#[test]
fn warm_archive_identity_ignores_reference_spelling_but_enforces_digest_and_platform() {
    let (bytes, mut image, _) = fixture();
    image.reference = format!("mirror.example/alias:other@{}", image.manifest_digest);
    let mut file = tempfile::tempfile().unwrap();
    std::io::Write::write_all(&mut file, &bytes).unwrap();
    let budget = TransferBudget {
        deadline: Instant::now() + Duration::from_secs(10),
        cancelled: &|| false,
    };
    verify_archive(&file, &image, "linux/arm64", bytes.len() as u64, &budget).unwrap();
    assert!(verify_archive(&file, &image, "linux/amd64", bytes.len() as u64, &budget).is_err());
    image.manifest_digest = format!("sha256:{}", "e".repeat(64));
    assert!(verify_archive(&file, &image, "linux/arm64", bytes.len() as u64, &budget).is_err());
}

#[test]
fn truncated_managed_layer_is_not_reused() {
    let (bytes, image, layer) = fixture();
    let source = Cursor::new(bytes[..layer.end as usize - 1].to_vec());
    let budget = TransferBudget {
        deadline: Instant::now() + Duration::from_secs(10),
        cancelled: &|| false,
    };
    assert!(inspect_archive(source, &image, "linux/arm64", &budget).is_err());
}
