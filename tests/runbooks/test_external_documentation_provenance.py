from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_nvidia_product_names_are_not_rebranded_as_vonk_products() -> None:
    forbidden = (
        "Vonk Forge OS",
        "Vonk Forge Dashboard",
        "NVIDIA's Vonk Forge GPU node container-runtime guide",
        "run on one Vonk Forge GPU node",
        "lists one Vonk Forge GPU node as the minimum",
        "NVIDIA-standard Cluster Profile switcher for Vonk Forge GPU node",
    )
    documentation = (ROOT / "README.md", *(ROOT / "docs").rglob("*.md"))

    for path in documentation:
        text = path.read_text()
        for false_product_name in forbidden:
            assert false_product_name not in text, (
                f"{path.relative_to(ROOT)} rebrands an NVIDIA product as Vonk Forge: "
                f"{false_product_name}"
            )


def test_historical_mia_docs_preserve_the_pinned_upstream_image_name() -> None:
    expected = "ghcr.io/anemll/dspark-vllm-gx10"
    for relative in (
        "docs/superpowers/specs/2026-08-01-dual-vonk-node-platform-design.md",
        "docs/superpowers/plans/2026-08-02-mia-deepseek-dual-runtime.md",
        "docs/superpowers/plans/2026-08-01-deepseek-0731-runtime.md",
    ):
        text = _read(relative)
        assert expected in text
        assert "ghcr.io/anemll/draft-vllm-gx10" not in text


def test_model_overviews_preserve_upstream_dspark_and_trellis_names() -> None:
    capacity = _read("docs/model-capacity-overview.md")
    assert "DSpark drafter pair" in capacity
    assert "`bleysg` DSpark work" in capacity
    assert "[DGX Spark Nemotron playbook](https://build.nvidia.com/spark/nemotron)" in capacity
    assert "[DGX Spark vLLM playbook](https://build.nvidia.com/spark/vllm/instructions)" in capacity
    assert "[dgx-trellis2](https://github.com/raziel2001au/dgx-trellis2)" in capacity
    assert (
        "[Trellis2-DGX-Spark-Docker]"
        "(https://github.com/dr-vij/Trellis2-DGX-Spark-Docker)"
    ) in capacity

    profile = _read("docs/model-profile-overview.md")
    assert "DS4 v0.5.3 Q2-imatrix + DSpark GGUF pair" in profile
    assert "`bleysg` DSpark work" in profile


def test_nvidia_document_links_keep_their_external_product_names() -> None:
    expected_by_file = {
        "docs/runbooks/fabric.md": (
            (
                "[NVIDIA DGX Spark clustering]"
                "(https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html)"
            ),
        ),
        "docs/runbooks/platform-update.md": (
            (
                "[DGX Spark release notes]"
                "(https://docs.nvidia.com/dgx/dgx-spark/release-notes.html)"
            ),
            (
                "[DGX Dashboard access]"
                "(https://docs.nvidia.com/dgx/dgx-spark/dgx-dashboard.html)"
            ),
            (
                "[DGX Spark system recovery]"
                "(https://docs.nvidia.com/dgx/dgx-spark/system-recovery.html)"
            ),
            (
                "[May 2026 DGX Spark security bulletin]"
                "(https://nvidia.custhelp.com/app/answers/detail/a_id/5835)"
            ),
        ),
        "docs/superpowers/specs/2026-08-03-existing-components-review.md": (
            "NVIDIA DGX Spark cloud-init/OEMDATA workflow",
            (
                "[NVIDIA DGX Spark Enterprise Manageability]"
                "(https://docs.nvidia.com/dgx/dgx-spark/enterprise-manageability.html)"
            ),
            (
                "[DGX Spark clustering and Cluster Assistant boundaries]"
                "(https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html)"
            ),
        ),
        "docs/superpowers/specs/2026-08-01-dual-vonk-node-platform-design.md": (
            "[NVIDIA DGX Spark user guide](https://docs.nvidia.com/dgx/dgx-spark/)",
            (
                "[NVIDIA DGX Spark update guide]"
                "(https://docs.nvidia.com/dgx/dgx-spark/os-and-component-update.html)"
            ),
            (
                "[NVIDIA two-Spark networking guide]"
                "(https://build.nvidia.com/spark/connect-two-sparks/stacked-sparks)"
            ),
        ),
    }
    for relative, expected_links in expected_by_file.items():
        text = _read(relative)
        for expected in expected_links:
            assert expected in text
