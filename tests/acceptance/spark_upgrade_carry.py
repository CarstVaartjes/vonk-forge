"""Upgrade-carry acceptance: a serving workload survives a release upgrade.

The lane installs the previous promoted release on a clean Spark runner and
starts the synthetic canary until its route serves inference. While the
canary keeps serving, the Controller is redeployed with the candidate release
and the Spark agent is upgraded in place to the candidate. A prober asks the
gateway for its models and runs one canary inference every few seconds the
whole time. The lane fails when the route drops, the model disappears from
the gateway or inference fails beyond a tiny tolerance. Then a new editorial
revision of the canary's recipe (same image, reworded description) is loaded
over it end to end on the candidate, and the canary's own stop and uninstall
run.

Release d30de9199 would have failed here: its new agent could not inspect a
run the previous agent started, the ranks went stale and the Controller
withdrew a route that was still serving.

Without a previous promoted release (the first ever) there is nothing to
carry; the lane says so with a visible notice and a "skipped" report.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[2]))

from cluster_profiles.serving_execution import (
    HttpObservation,
    ServingExecutionError,
    evaluate_http_response,
)
from scripts.development_slice_client import SliceError, require_object
from tests.acceptance.runtime import (
    AcceptanceError,
    assert_compose_services_healthy,
)
from tests.acceptance.test_fresh_nas_install import generate_bundle
from tests.acceptance.test_spark_lifecycle import (
    CHANNEL,
    LOCAL_CONTROLLER_SERVICES,
    REPOSITORY_ROOT,
    SHA256,
    SOURCE_SHA,
    LifecycleError,
    LocalBrowserController,
    SparkLifecycle,
    _atomic_write,
    _canonical,
    _editorial_successor,
    _run_spark_bootstrap,
)

PROBE_INTERVAL_SECONDS = 3.0
# Longer than two route-evidence windows (120 s) plus a report cycle: a
# Controller that withdraws routes on stale rank evidence does it inside this.
AGENT_SETTLE_SECONDS = 300
CONTROLLER_SETTLE_SECONDS = 60
BASELINE_SERVING_SECONDS = 30
# Outside the Controller recreate (the gateway restarts with it) a carried
# workload may miss at most this many probes, never two in a row.
TOLERATED_PROBE_FAILURES = 1
OVERLAY_VARIABLE = "VONK_ACCEPTANCE_COMPOSE_OVERLAY"


@dataclass
class ProbeResult:
    at: float
    phase: str
    models_listed: bool
    inference_ok: bool
    route_state: str | None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.models_listed and self.inference_ok


@dataclass
class ReleaseInput:
    generation: str
    source_sha: str
    caddyfile: str
    release: Path
    signature: Path
    overlay: Path
    version: str
    package_version: str


@dataclass
class CarryEvidence:
    phases: list[dict[str, object]] = field(default_factory=list)
    probes: list[ProbeResult] = field(default_factory=list)


def _fetch(url: str, destination: Path) -> None:
    # The public origin refuses anonymous library user agents.
    request = urllib.request.Request(
        url, headers={"User-Agent": "vonk-forge-acceptance/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            destination.write_bytes(response.read(16 * 1024 * 1024))
    except urllib.error.URLError as error:
        raise LifecycleError(f"release input {url} is unavailable: {error}") from error


def resolve_release(
    origin: str, channel: str, generation: str, root: Path
) -> ReleaseInput:
    """Download one published release and render its accepted image overlay."""

    if SHA256.fullmatch(generation) is None:
        raise LifecycleError("release generation is invalid")
    directory = root / generation
    directory.mkdir(parents=True, exist_ok=True)
    base = f"{origin}/artifacts/{channel}/releases/{generation}"
    release = directory / "release.json"
    signature = directory / "release.sig"
    _fetch(f"{base}/release.json", release)
    _fetch(f"{base}/release.sig", signature)
    overlay = directory / "accepted-compose-overlay.yml"
    # The renderer verifies the release signature before pinning its images.
    rendered = subprocess.run(
        [
            sys.executable,
            os.fspath(REPOSITORY_ROOT / "scripts/render-accepted-compose-overlay"),
            "--release",
            os.fspath(release),
            "--signature",
            os.fspath(signature),
            "--public-key",
            os.fspath(REPOSITORY_ROOT / "install/installer-release-public.pem"),
            "--channel",
            channel,
            "--generation",
            generation,
            "--output",
            os.fspath(overlay),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if rendered.returncode != 0:
        raise LifecycleError(
            f"release {generation} is not an accepted release: "
            f"{(rendered.stderr or rendered.stdout).strip()[-400:]}"
        )
    document = json.loads(release.read_text(encoding="utf-8"))
    package = require_object(
        require_object(document.get("artifacts"), "release artifacts").get(
            "agent-package-linux-arm64"
        ),
        "release agent package",
    )
    source_sha = str(document.get("source_sha"))
    if SOURCE_SHA.fullmatch(source_sha) is None:
        raise LifecycleError(f"release {generation} names no source commit")
    caddy = directory / "Caddyfile"
    # The acceptance Caddyfile must be the one this release ships.
    _fetch(
        "https://raw.githubusercontent.com/CarstVaartjes/vonk-forge/"
        f"{source_sha}/deploy/compose/Caddyfile",
        caddy,
    )
    return ReleaseInput(
        generation=generation,
        source_sha=source_sha,
        caddyfile=caddy.read_text(encoding="utf-8"),
        release=release,
        signature=signature,
        overlay=overlay,
        version=str(document.get("version")),
        package_version=str(package.get("package_version")),
    )


class UpgradeCarryLifecycle(SparkLifecycle):
    """The Spark lifecycle, started on the previous release and upgraded live."""

    def __init__(
        self,
        arguments: argparse.Namespace,
        *,
        baseline: ReleaseInput,
        candidate: ReleaseInput,
    ) -> None:
        super().__init__(arguments, {})
        self.baseline = baseline
        self.candidate = candidate
        self.controller_generation = baseline.generation
        self.controller_release = baseline.release
        os.environ[OVERLAY_VARIABLE] = os.fspath(baseline.overlay)
        self.evidence = CarryEvidence()
        self.failure_evidence: dict[str, object] | None = None
        self._phase = "baseline-install"
        self._successor_identity: tuple[str, str] = ("", "")
        self._probing = threading.Event()
        self._prober: threading.Thread | None = None
        self._lock = threading.Lock()
        self._serving_digest: str | None = None

    # The previous release's Fleet document is read as plain JSON: this lane
    # judges serving behaviour, not one release's response schema.
    def _fleet_snapshot(self) -> dict[str, object]:
        assert self.control is not None
        _, payload = self.control.request("GET", "/api/fleet")
        return require_object(payload, "Fleet snapshot")

    def _await_profile_application(
        self, operation: dict[str, object], *, label: str, node_id: str
    ) -> dict[str, object]:
        """Follow a profile application by its stable fields only.

        The previous release answers in its own schema; the lane needs the
        identity, the state and the receipts it reads afterwards.
        """
        del node_id
        assert self.control is not None
        application = require_object(operation, label)
        application_id = application.get("id")
        deadline = time.monotonic() + 1800
        while application.get("state") in {"queued", "running", "waiting-for-operator"}:
            if time.monotonic() >= deadline:
                raise LifecycleError(
                    f"{label} did not converge: state={application.get('state')} "
                    f"reason={application.get('status_reason')}"
                )
            time.sleep(1)
            _, payload = self.control.request(
                "GET", f"/api/profile/applications/{application_id}"
            )
            application = require_object(payload, label)
            if application.get("id") != application_id:
                raise LifecycleError(f"{label} identifies a different application")
        if application.get("state") != "succeeded":
            raise LifecycleError(
                f"{label} failed: state={application.get('state')} "
                f"reason={application.get('status_reason')}"
            )
        return application

    def _acceptance_caddyfile(self) -> str | None:
        current = (
            self.candidate
            if self.controller_generation == self.candidate.generation
            else self.baseline
        )
        return current.caddyfile

    def _release_environment(self, release: ReleaseInput) -> dict[str, str]:
        assert self.temporary_root is not None
        base = (
            f"{self.origin}/artifacts/{self.arguments.channel}/releases/"
            f"{release.generation}"
        )
        return {
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "TMPDIR": os.fspath(self.temporary_root),
            "VONK_CONTROLLER_ADDRESS": "127.0.0.1",
            "VONK_INSTALL_BASE_URL": base,
            "VONK_INSTALL_RELEASE_MANIFEST": os.fspath(release.release),
            "VONK_INSTALL_RELEASE_SIGNATURE": os.fspath(release.signature),
            **getattr(self, "firewall_environment", {}),
        }

    def _spark_bootstrap_url(self, release: ReleaseInput) -> str:
        return (
            f"{self.origin}/artifacts/{self.arguments.channel}/releases/"
            f"{release.generation}/bootstraps/spark"
        )

    def observe(self) -> dict[str, object]:
        if self.control is None or self.bundle is None or self.temporary_root is None:
            raise LifecycleError("previous-release controller is not ready")
        grant_id, enrollment_url, ca_sha256, pairing_token = self._create_grant()
        del grant_id
        try:
            _run_spark_bootstrap(
                self._spark_bootstrap_url(self.baseline),
                cwd=self.temporary_root,
                environment=self._release_environment(self.baseline),
                enrollment_url=enrollment_url,
                ca_sha256=ca_sha256,
                pairing_token=pairing_token,
            )
        except AcceptanceError as error:
            raise self._installation_failure(
                "previous-release Spark installation", error
            ) from error
        finally:
            del pairing_token
        self.agent_installed = True
        self._prepare_podman_apparmor_profile()
        identity = self._wait_for_agent_identity(
            package_version=self.baseline.package_version, timeout=180
        )
        node_id = str(identity["node_id"])
        canary = self._run_synthetic_canary(node_id, carry=self._carry)
        return {
            "baseline": {
                "generation": self.baseline.generation,
                "version": self.baseline.version,
            },
            "candidate": {
                "generation": self.candidate.generation,
                "version": self.candidate.version,
            },
            "canary": canary,
            "phases": self.evidence.phases,
            "probe_summary": self._probe_summary(),
        }

    # -- the carry ---------------------------------------------------------

    def _carry(self) -> tuple[str, str]:
        fixture = self.synthetic_canary_fixture
        self._alias = fixture.slug
        self._serving_check = fixture.serving_check
        self._inference_key = self._read_secret("litellm-master-key")
        self._start_prober()
        try:
            self._hold("baseline-serving", BASELINE_SERVING_SECONDS)
            self._run_phase("controller-redeploy", self._redeploy_controller)
            self._hold("controller-settled", CONTROLLER_SETTLE_SECONDS)
            self._run_phase("agent-upgrade", self._upgrade_agent)
            self._hold("agent-settled", AGENT_SETTLE_SECONDS)
            self._run_phase("route-still-published", self._require_published)
        finally:
            self._stop_prober()
        self._judge()
        # The carried workload is judged; now a new revision of its recipe
        # replaces it, which is what the hourly recipe refresh does.
        self._run_phase("editorial-successor", self._load_editorial_successor)
        return self._successor_identity

    def _run_phase(self, name: str, action) -> None:
        started = time.monotonic()
        with self._lock:
            self._phase = name
        try:
            action()
        except (LifecycleError, AcceptanceError, SliceError) as error:
            raise self._failure(name, str(error)) from error
        self.evidence.phases.append(
            {"phase": name, "seconds": round(time.monotonic() - started, 1)}
        )

    def _hold(self, name: str, seconds: float) -> None:
        self._run_phase(name, lambda: time.sleep(seconds))

    def _redeploy_controller(self) -> None:
        """Upgrade the Controller the way an operator reruns the installer."""

        assert self.temporary_root is not None and self.bundle is not None
        os.environ[OVERLAY_VARIABLE] = os.fspath(self.candidate.overlay)
        self.controller_generation = self.candidate.generation
        self.controller_release = self.candidate.release
        child_environment, responses = self._controller_inputs
        # The installer upgrades only a bundle it recognises; the lane's own
        # Caddyfile copy is laid down again after it.
        (self.bundle / "acceptance-Caddyfile").unlink(missing_ok=True)
        generate_bundle(
            self.temporary_root / "controller",
            candidate_url=(
                f"{self.origin}/artifacts/{self.arguments.channel}/releases/"
                f"{self.candidate.generation}/bootstraps/nas"
            ),
            child_environment=child_environment,
            responses=responses,
            require_all_prompts=False,
        )
        self._reapply_controller_site()
        self._assert_compose_image_graph()
        self._run_command(
            self._local_controller_up_command(),
            cwd=self.bundle,
            timeout=420,
            report_failure_output=True,
        )
        status = self._run_command(
            self._compose("ps", "--all", "--format", "json"), cwd=self.bundle
        )
        try:
            assert_compose_services_healthy(status.stdout, LOCAL_CONTROLLER_SERVICES)
        except AcceptanceError as error:
            raise LifecycleError(
                "candidate controller services are not healthy after the upgrade"
            ) from error
        self._assert_running_publication_images()
        browser = LocalBrowserController(
            hostname=self.control_hostname, port=self._local_browser_port()
        )
        password = self._read_secret("admin-password")
        control = browser.login(password, timeout=30)
        del password
        with self._lock:
            self.browser = browser
            self.control = control

    def _upgrade_agent(self) -> None:
        """Upgrade the Spark agent in place with the candidate installer."""

        assert self.temporary_root is not None
        try:
            _run_spark_bootstrap(
                self._spark_bootstrap_url(self.candidate),
                cwd=self.temporary_root,
                environment=self._release_environment(self.candidate),
            )
        except AcceptanceError as error:
            raise LifecycleError(f"candidate Spark upgrade failed: {error}") from error
        self._wait_for_agent_identity(
            package_version=self.candidate.package_version, timeout=300
        )

    def _load_editorial_successor(self) -> None:
        """Load a new editorial revision of the running recipe, end to end.

        The successor changes only the description, so its image, build and
        model are the running workload's. It goes through the owner's path:
        catalog sync, recipe download, profile review, admission, install plan,
        install and start, and must serve inference as the new revision.
        """

        assert self.control is not None and self.browser is not None
        fixture = self.synthetic_canary_fixture
        successor = _editorial_successor(fixture)
        nodes = self._fleet_snapshot().get("nodes")
        node_ids = [
            node["id"]
            for node in (nodes if isinstance(nodes, list) else [])
            if isinstance(node, dict) and isinstance(node.get("id"), str)
        ]
        if len(node_ids) != 1:
            raise LifecycleError("the editorial successor needs exactly one Spark")
        node_id = node_ids[0]
        sync = self._import_canary_catalog(
            successor, self._canary_request_key(successor, node_id, "editorial-sync")
        )
        if (
            sync.get("state") != "current"
            or sync.get("commit") != successor.source_commit
            or sync.get("problems") != []
        ):
            raise LifecycleError(
                "editorial successor catalog sync is incomplete: "
                + json.dumps(sync, sort_keys=True, default=str)[:1024]
            )
        selector = f"{successor.publisher}/{successor.slug}"
        _, detail_payload = self.control.request("GET", f"/api/recipe/{selector}")
        identity = require_object(
            require_object(detail_payload, "editorial successor detail").get(
                "identity"
            ),
            "editorial successor identity",
        )
        revision_id = identity.get("recipe_revision_id")
        if identity.get("content_sha256") != successor.recipe_content_sha256 or not (
            isinstance(revision_id, str)
        ):
            raise LifecycleError("the editorial successor is not the newest revision")
        download = self._await_recipe_download(
            self._request_recipe_download(
                selector,
                request_key=self._canary_request_key(
                    successor, node_id, "editorial-download"
                ),
            ),
            fixture=successor,
            recipe_revision_id=revision_id,
        )
        if download.get("state") != "succeeded":
            raise LifecycleError("the editorial successor download did not succeed")
        _, preview_payload = self.control.request("POST", "/api/profile/1/preview")
        preview = require_object(preview_payload, "editorial successor preview")
        if preview.get("allowed") is not True:
            raise LifecycleError(
                "editorial successor profile preview is not admitted: "
                + self._preview_diagnostic(preview)
            )
        application = self._await_profile_application(
            require_object(
                self._load_canary_profile(
                    preview,
                    request_key=self._canary_request_key(
                        successor, node_id, "editorial-load"
                    ),
                ),
                "editorial successor profile application",
            ),
            label="editorial successor profile load",
            node_id=node_id,
        )
        if application.get("state") != "succeeded":
            raise LifecycleError(
                "editorial successor profile load did not succeed: "
                + str(application.get("status_reason"))[:512]
            )
        self._await_canary_endpoint(successor.slug, published=True)
        inference = self.browser.bearer(self._inference_key, timeout=30)
        self._run_canonical_inference(
            inference, successor.serving_check, successor.slug
        )
        progress = require_object(application.get("progress"), "successor progress")
        run_result = self._profile_run_switch_result(
            require_object(progress.get("step_results"), "successor step results")
        )
        phase_results = run_result.get("phase_results")
        if not isinstance(phase_results, list):
            raise LifecycleError("the editorial successor run receipt is invalid")
        self._successor_identity = self._serving_identity(phase_results)

    def _require_published(self) -> None:
        state = self._route_state()
        if state != "published":
            raise LifecycleError(
                f"the carried workload's route is {state!r}, not published"
            )

    # -- probing -----------------------------------------------------------

    def _start_prober(self) -> None:
        self._probing.set()
        self._prober = threading.Thread(target=self._probe_loop, daemon=True)
        self._prober.start()

    def _stop_prober(self) -> None:
        self._probing.clear()
        if self._prober is not None:
            self._prober.join(timeout=60)

    def _probe_loop(self) -> None:
        while self._probing.is_set():
            self.evidence.probes.append(self._probe())
            time.sleep(PROBE_INTERVAL_SECONDS)

    def _probe(self) -> ProbeResult:
        with self._lock:
            phase = self._phase
            browser = self.browser
        at = time.monotonic()
        models_listed = inference_ok = False
        detail: str | None = None
        route_state = None
        try:
            assert browser is not None
            inference = browser.bearer(self._inference_key, timeout=20)
            status, payload = inference.request("GET", "/v1/models")
            listed = require_object(payload, "gateway models").get("data")
            models_listed = status == 200 and any(
                isinstance(item, dict) and item.get("id") == self._alias
                for item in (listed if isinstance(listed, list) else [])
            )
            if not models_listed:
                detail = "gateway does not list the carried model"
            path, body = self._serving_request(self._serving_check, self._alias)
            status, payload = inference.request("POST", path, body)
            response = require_object(payload, "synthetic serving response")
            evaluate_http_response(
                HttpObservation(status=status, headers={}, body=_canonical(response)),
                self._serving_check,
            )
            digest = hashlib.sha256(_canonical(response)).hexdigest()
            if self._serving_digest is None:
                self._serving_digest = digest
            inference_ok = digest == self._serving_digest
            if not inference_ok:
                detail = "inference answered differently than before the upgrade"
        except (
            LifecycleError,
            SliceError,
            ServingExecutionError,
            AssertionError,
        ) as error:
            detail = detail or f"{type(error).__name__}: {str(error)[:200]}"
        try:
            route_state = self._route_state()
        except (LifecycleError, SliceError, AssertionError):
            route_state = None
        return ProbeResult(
            at=at,
            phase=phase,
            models_listed=models_listed,
            inference_ok=inference_ok,
            route_state=route_state,
            detail=detail,
        )

    def _route_state(self) -> str | None:
        with self._lock:
            control = self.control
        assert control is not None
        _, payload = control.request("GET", "/api/fleet")
        for node in require_object(payload, "Fleet snapshot").get("nodes") or []:
            for run in (node.get("loaded") or []) if isinstance(node, dict) else []:
                if isinstance(run, dict) and run.get("alias") == self._alias:
                    state = run.get("route_state")
                    return str(state) if state is not None else None
        return "absent"

    def _judge(self) -> None:
        """Fail on any loss of service outside the Controller recreate."""

        outside = [
            probe
            for probe in self.evidence.probes
            if probe.phase not in {"controller-redeploy"}
        ]
        failures = [probe for probe in outside if not probe.ok]
        consecutive = any(
            not first.ok and not second.ok
            for first, second in itertools.pairwise(outside)
        )
        withdrawn = [
            probe
            for probe in outside
            if probe.route_state is not None and probe.route_state != "published"
        ]
        if withdrawn:
            raise self._failure(
                withdrawn[0].phase,
                f"the route was {withdrawn[0].route_state!r} while the workload served",
            )
        if len(failures) > TOLERATED_PROBE_FAILURES or consecutive:
            raise self._failure(
                failures[0].phase,
                f"{len(failures)} failed probes outside the Controller recreate",
            )
        if not any(probe.phase == "agent-settled" and probe.ok for probe in outside):
            raise self._failure(
                "agent-settled", "no successful probe after the upgrade"
            )

    def _probe_summary(self) -> dict[str, object]:
        phases: dict[str, dict[str, int]] = {}
        for probe in self.evidence.probes:
            entry = phases.setdefault(probe.phase, {"probes": 0, "failed": 0})
            entry["probes"] += 1
            entry["failed"] += 0 if probe.ok else 1
        failing = [asdict(probe) for probe in self.evidence.probes if not probe.ok]
        return {"by_phase": phases, "failures": failing[:3] + failing[-3:]}

    def _failure(self, phase: str, reason: str) -> LifecycleError:
        """A short verdict that names the phase; the full evidence is logged.

        The canary wrapper keeps only the end of a long message, so the
        verdict stays brief and the probe evidence goes to the log and the
        report as well.
        """
        summary = self._probe_summary()
        self.failure_evidence = {"phase": phase, "reason": reason, **summary}
        print(
            "upgrade-carry probe evidence: " + json.dumps(summary, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )
        counts = ", ".join(
            f"{name} {entry['failed']}/{entry['probes']} failed"
            for name, entry in summary["by_phase"].items()  # type: ignore[union-attr]
        )
        return LifecycleError(
            f"upgrade-carry failed in phase {phase}: {reason} (probes: {counts})"
        )


def _skipped(output: Path, reason: str) -> int:
    print(f"::notice title=Upgrade-carry acceptance skipped::{reason}")
    _atomic_write(output, {"schema_version": 1, "status": "skipped", "reason": reason})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Carry a serving workload across a release upgrade."
    )
    parser.add_argument("--channel", required=True)
    parser.add_argument("--candidate-generation", required=True)
    parser.add_argument("--baseline-generation", default="")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--platform", default="linux-arm64")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if CHANNEL.fullmatch(arguments.channel) is None:
        print("upgrade-carry inputs are invalid", file=sys.stderr)
        return 1
    if not arguments.baseline_generation:
        return _skipped(
            arguments.output,
            f"no previous promoted {arguments.channel} release exists; there is "
            "no running workload to carry across an upgrade",
        )
    if arguments.baseline_generation == arguments.candidate_generation:
        return _skipped(
            arguments.output,
            "the previous promoted release is the candidate itself",
        )
    origin = os.environ.get("INSTALLER_PUBLIC_ORIGIN", "")
    lifecycle_run: UpgradeCarryLifecycle | None = None
    workspace = Path(os.environ.get("VONK_ACCEPTANCE_WORKSPACE", "."))
    try:
        inputs = workspace / "upgrade-carry-releases"
        baseline = resolve_release(
            origin, arguments.channel, arguments.baseline_generation, inputs
        )
        candidate = resolve_release(
            origin, arguments.channel, arguments.candidate_generation, inputs
        )
        lane = argparse.Namespace(
            channel=arguments.channel,
            generation=candidate.generation,
            candidate_release=candidate.release,
            baseline_release=baseline.release,
            run_id=arguments.run_id,
            platform=arguments.platform,
        )
        lifecycle_run = UpgradeCarryLifecycle(
            lane, baseline=baseline, candidate=candidate
        )
        with lifecycle_run as lifecycle:
            proof = lifecycle.observe()
    except LifecycleError as error:
        print(f"Spark upgrade-carry acceptance failed: {error}", file=sys.stderr)
        _atomic_write(
            arguments.output,
            {
                "schema_version": 1,
                "status": "failed",
                "error": str(error)[-2000:],
                "evidence": getattr(lifecycle_run, "failure_evidence", None),
            },
        )
        return 1
    _atomic_write(
        arguments.output,
        {"schema_version": 1, "status": "passed", "proof": proof},
    )
    print(json.dumps(proof["probe_summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
