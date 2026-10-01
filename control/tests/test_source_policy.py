from __future__ import annotations

import copy

import pytest
from vonk_control.recipe_builds import inspect_package_source_policy
from vonk_control.source_bundles import generate_source_bundle, parse_source_bundle
from vonk_control.source_policy import SourcePolicyError, enforce_build_source_policy

from tests.canonical_recipe_fixtures import canonical_example, source_policy_recipe


@pytest.fixture
def recipe() -> dict[str, object]:
    return source_policy_recipe()


def bundle_for(recipe: dict[str, object], dockerfile: str, **files: bytes):
    bundle = generate_source_bundle({"Dockerfile": dockerfile.encode(), **files})
    build = recipe["build"]
    assert isinstance(build, dict)
    context = build["context"]
    assert isinstance(context, dict)
    context["sha256"] = bundle.sha256
    context["expected_bytes"] = len(bundle.archive)
    return bundle


def test_digest_pinned_non_root_source_passes(recipe: dict[str, object]) -> None:
    bundle = bundle_for(
        recipe,
        "FROM ghcr.io/example/vllm@sha256:" + "a" * 64 + "\nUSER 10001:10001\n",
    )

    report = enforce_build_source_policy(recipe, bundle)

    assert report.passed is True
    assert report.dockerfile == "Dockerfile"


def test_absolute_copy_source_from_named_stage_passes(
    recipe: dict[str, object],
) -> None:
    bundle = bundle_for(
        recipe,
        "FROM docker.io/library/busybox@sha256:"
        + "a" * 64
        + " AS tools\n"
        + "FROM ghcr.io/example/runtime@sha256:"
        + "b" * 64
        + "\nCOPY --from=tools /bin/busybox /opt/runtime/busybox\n"
        + "USER 10001:10001\n",
    )

    report = enforce_build_source_policy(recipe, bundle)

    assert report.passed is True


@pytest.mark.parametrize(
    ("dockerfile", "code"),
    [
        ("FROM ghcr.io/example/vllm:latest\nUSER 10001\n", "dockerfile.base_unpinned"),
        (
            "FROM ghcr.io/example/vllm@sha256:" + "0" * 64 + "\nUSER 10001\n",
            "dockerfile.base_placeholder",
        ),
        (
            "FROM ghcr.io/example/x@sha256:"
            + "a" * 64
            + "\nADD https://evil.invalid/x /x\nUSER 10001\n",
            "dockerfile.add_forbidden",
        ),
        (
            "FROM ghcr.io/example/x@sha256:"
            + "a" * 64
            + "\nRUN --mount=type=secret echo x\nUSER 10001\n",
            "dockerfile.secret_mount",
        ),
        (
            "FROM ghcr.io/example/x@sha256:"
            + "a" * 64
            + "\nCOPY /etc/passwd /opt/runtime/passwd\nUSER 10001\n",
            "dockerfile.copy_path",
        ),
        (
            "FROM ghcr.io/example/x@sha256:" + "a" * 64 + "\nUSER root\n",
            "dockerfile.root_user",
        ),
    ],
)
def test_unsafe_dockerfile_is_rejected(
    recipe: dict[str, object], dockerfile: str, code: str
) -> None:
    bundle = bundle_for(recipe, dockerfile)

    with pytest.raises(SourcePolicyError) as caught:
        enforce_build_source_policy(recipe, bundle)

    assert caught.value.report.findings[0].code == code


def test_unsafe_compose_is_rejected(recipe: dict[str, object]) -> None:
    compose = (
        b"services:\n  model:\n    privileged: true\n    volumes:\n      - /:/host\n"
    )
    bundle = bundle_for(
        recipe,
        "FROM ghcr.io/example/x@sha256:" + "a" * 64 + "\nUSER 10001\n",
        **{"compose.yaml": compose},
    )

    with pytest.raises(SourcePolicyError) as caught:
        enforce_build_source_policy(recipe, bundle)

    assert {finding.code for finding in caught.value.report.findings} == {
        "compose.host_bind",
        "compose.privileged",
    }


def test_bundle_identity_must_match_recipe(recipe: dict[str, object]) -> None:
    bundle = bundle_for(
        recipe,
        "FROM ghcr.io/example/x@sha256:" + "a" * 64 + "\nUSER 10001\n",
    )
    changed = copy.deepcopy(recipe)
    changed_build = changed["build"]
    assert isinstance(changed_build, dict)
    changed_context = changed_build["context"]
    assert isinstance(changed_context, dict)
    changed_context["sha256"] = "b" * 64

    with pytest.raises(SourcePolicyError) as caught:
        enforce_build_source_policy(changed, bundle)

    assert caught.value.report.findings[0].code == "source.digest_mismatch"


def test_public_build_refuses_a_url_outside_the_declared_host_allowlist(
    recipe: dict[str, object],
) -> None:
    recipe_build = recipe["build"]
    assert isinstance(recipe_build, dict)
    recipe_build["network"] = {
        "mode": "public",
        "hosts": ["archives.example"],
    }
    bundle = bundle_for(
        recipe,
        "FROM ghcr.io/example/x@sha256:"
        + "a" * 64
        + "\nRUN curl --fail https://undeclared.example/source.tar.gz -o /tmp/source\n"
        + "USER 10001\n",
    )

    with pytest.raises(SourcePolicyError) as caught:
        enforce_build_source_policy(recipe, bundle)

    assert caught.value.report.findings[0].code == "dockerfile.network_host"


def test_public_build_accepts_only_urls_on_the_declared_host_allowlist(
    recipe: dict[str, object],
) -> None:
    recipe_build = recipe["build"]
    assert isinstance(recipe_build, dict)
    recipe_build["network"] = {
        "mode": "public",
        "hosts": ["archives.example"],
    }
    bundle = bundle_for(
        recipe,
        "FROM ghcr.io/example/x@sha256:"
        + "a" * 64
        + "\nRUN curl --fail https://archives.example/source.tar.gz -o /tmp/source\n"
        + "USER 10001\n",
    )

    assert enforce_build_source_policy(recipe, bundle).passed is True


def _package_bundle(dockerfile: str, **files: bytes):
    """A library package's build context, as CI and validation read it."""

    document = canonical_example("recipe-source-build.json")
    build = document["execution"]["build"]
    relative = build["dockerfile"].removeprefix(
        build["context"]["path"].rstrip("/") + "/"
    )
    bundle = generate_source_bundle({relative: dockerfile.encode(), **files})
    return document, parse_source_bundle(bundle.archive)


_BASE = "FROM ghcr.io/example/vllm@sha256:" + "a" * 64 + "\n"


def test_package_policy_accepts_what_the_controller_accepts() -> None:
    document, bundle = _package_bundle(_BASE + "USER 10001:10001\n")

    assert inspect_package_source_policy(document, bundle).passed is True


def test_package_policy_refuses_what_the_controller_refuses() -> None:
    document, bundle = _package_bundle(
        _BASE + "RUN cat > /etc/x <<EOF\nx\nEOF\nUSER 10001:10001\n"
    )

    report = inspect_package_source_policy(document, bundle)

    assert [finding.code for finding in report.findings] == [
        "dockerfile.heredoc_forbidden"
    ]
    assert "dockerfile.heredoc_forbidden" in report.describe()


def test_package_policy_refuses_a_bundle_the_catalog_does_not_name() -> None:
    document, bundle = _package_bundle(_BASE + "USER 10001:10001\n")

    report = inspect_package_source_policy(document, bundle, source_sha256="f" * 64)

    assert [finding.code for finding in report.findings] == ["source.digest_mismatch"]


_HEREDOC_BASE = "FROM ghcr.io/example/vllm@sha256:" + "a" * 64 + "\n"


@pytest.mark.parametrize(
    "line",
    [
        "RUN <<EOF\nx\nEOF",
        "RUN <<-EOF\nx\nEOF",
        "RUN <<'EOF'\nx\nEOF",
        'RUN <<"EOF"\nx\nEOF',
        "RUN python3 <<PY\nx\nPY",
        "RUN cat <<EOF > /x\nx\nEOF",
        "RUN cat<<EOF\nx\nEOF",
        "COPY <<EOF /x\nx\nEOF",
        "ADD <<EOF /x\nx\nEOF",
    ],
)
def test_real_heredocs_are_refused(recipe: dict[str, object], line: str) -> None:
    bundle = bundle_for(recipe, _HEREDOC_BASE + line + "\nUSER 10001:10001\n")

    with pytest.raises(SourcePolicyError) as caught:
        enforce_build_source_policy(recipe, bundle)

    assert "dockerfile.heredoc_forbidden" in {
        finding.code for finding in caught.value.report.findings
    }


@pytest.mark.parametrize(
    "line",
    [
        "RUN awk '/<<.PY.$/' /x",
        'RUN awk "/<<EOF/" /x',
        "RUN sed -n '/<<-EOF/p' /x",
        "RUN grep -q '<<EOF' /x",
        'RUN ["sh", "-c", "grep <<EOF /x"]',
        # Here-strings: BuildKit's heredoc word regex excludes "<", so these are
        # plain shell redirections, not Dockerfile heredocs.
        'RUN cat <<<"x"',
        "RUN bash -c 'cat <<<x'",
        "RUN echo $((1<<2))",
        "RUN echo a << ",
    ],
)
def test_non_heredoc_double_angle_is_accepted(
    recipe: dict[str, object], line: str
) -> None:
    bundle = bundle_for(recipe, _HEREDOC_BASE + line + "\nUSER 10001:10001\n")

    report = enforce_build_source_policy(recipe, bundle)

    assert report.passed is True
