"""Policy-limited LiteLLM configuration derived only from published routes."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .routes import RouteState

_UPSTREAM_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,119}\Z")


class LiteLlmPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class LiteLlmDeployment:
    model_name: str
    workload: str
    api_base: str
    priority: int
    requests_per_minute: int
    tokens_per_minute: int


@dataclass(frozen=True)
class LiteLlmPolicy:
    models: Mapping[str, Mapping[str, int | str]]
    deployments: tuple[LiteLlmDeployment, ...] = ()


@dataclass(frozen=True)
class LiteLlmGeneration:
    generation: int
    route_digest: str
    config_sha256: str
    path: str


class LiteLlmPublisher:
    def __init__(self, root: Path, *, validate: Callable[[bytes], bool], apply: Callable[[bytes], None]) -> None:
        if root.is_symlink():
            raise LiteLlmPolicyError("LiteLLM state root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root = root
        self._validate = validate
        self._apply = apply

    @staticmethod
    def render(routes: RouteState, policy: LiteLlmPolicy) -> bytes:
        if routes.state != "published" or not routes.aliases:
            raise LiteLlmPolicyError("LiteLLM models require a published route snapshot")
        if any(not isinstance(value, str) or not value for value in routes.aliases.values()):
            raise LiteLlmPolicyError("LiteLLM routes must be already-rendered strings")
        models = dict(policy.models)
        unknown = set(models) - set(routes.aliases)
        if unknown:
            raise LiteLlmPolicyError("LiteLLM policy contains models outside published aliases")
        if not models and not policy.deployments:
            raise LiteLlmPolicyError("LiteLLM policy must publish at least one model")
        model_list = []
        for alias in sorted(models):
            quota = dict(models[alias])
            required = {"requests_per_minute", "tokens_per_minute"}
            fields = set(quota)
            if fields not in (required, required | {"upstream_model"}):
                raise LiteLlmPolicyError("LiteLLM model quota fields are invalid")
            upstream_model = quota.pop("upstream_model", alias)
            if (
                not isinstance(upstream_model, str)
                or _UPSTREAM_MODEL.fullmatch(upstream_model) is None
            ):
                raise LiteLlmPolicyError("LiteLLM upstream model is invalid")
            rpm, tpm = quota["requests_per_minute"], quota["tokens_per_minute"]
            if not isinstance(rpm, int) or not isinstance(tpm, int) or not 1 <= rpm <= 100_000 or not 1 <= tpm <= 100_000_000:
                raise LiteLlmPolicyError("LiteLLM model quotas are outside allowed bounds")
            model_list.append({
                "model_name": alias,
                "litellm_params": {
                    "model": f"openai/{upstream_model}",
                    "api_base": routes.aliases[alias].rstrip("/"),
                    "api_key": "os.environ/LITELLM_UPSTREAM_KEY",
                    "rpm": rpm,
                    "tpm": tpm,
                },
            })
        deployments = sorted(policy.deployments, key=lambda item: item.priority)
        if len({item.priority for item in deployments}) != len(deployments):
            raise LiteLlmPolicyError("Hermes deployment priorities must be unique")
        if len({item.workload for item in deployments}) != len(deployments):
            raise LiteLlmPolicyError("Hermes deployment workloads must be unique")
        for deployment in deployments:
            LiteLlmPublisher._validate_hermes_deployment(deployment)
            model_list.append({
                "model_name": deployment.model_name,
                "litellm_params": {
                    "model": f"openai/{deployment.workload}",
                    "api_base": deployment.api_base,
                    "api_key": "os.environ/LITELLM_UPSTREAM_KEY",
                    "order": deployment.priority,
                    "rpm": deployment.requests_per_minute,
                    "tpm": deployment.tokens_per_minute,
                },
            })
        router_settings = {
            "enable_pre_call_checks": True,
            "routing_strategy": "simple-shuffle",
        }
        if deployments:
            router_settings.update({
                "allowed_fails": 0,
                "num_retries": 1,
                "retry_policy": {
                    "AuthenticationErrorRetries": 0,
                    "BadRequestErrorRetries": 0,
                    "ContentPolicyViolationErrorRetries": 0,
                    "RateLimitErrorRetries": 1,
                    "TimeoutErrorRetries": 1,
                },
            })
        document = {
            "general_settings": {
                "database_url": "os.environ/LITELLM_DATABASE_URL",
                "disable_admin_ui": False,
                "master_key": "os.environ/LITELLM_MASTER_KEY",
                "store_model_in_db": False,
            },
            "litellm_settings": {
                "drop_params": True,
                "set_verbose": False,
                "success_callback": [],
                "failure_callback": [],
            },
            "model_list": model_list,
            "router_settings": router_settings,
        }
        return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()

    @staticmethod
    def _validate_hermes_deployment(deployment: LiteLlmDeployment) -> None:
        if deployment.model_name != "hermes-agent":
            raise LiteLlmPolicyError("Hermes deployment alias must be hermes-agent")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", deployment.workload):
            raise LiteLlmPolicyError("Hermes deployment workload is invalid")
        if isinstance(deployment.priority, bool) or not isinstance(deployment.priority, int) or deployment.priority < 1:
            raise LiteLlmPolicyError("Hermes deployment priority is invalid")
        for value in (deployment.requests_per_minute, deployment.tokens_per_minute):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise LiteLlmPolicyError("Hermes deployment quota is invalid")
        if deployment.requests_per_minute > 100_000 or deployment.tokens_per_minute > 100_000_000:
            raise LiteLlmPolicyError("Hermes deployment quota is invalid")
        try:
            parsed = urlsplit(deployment.api_base)
            address = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError as error:
            raise LiteLlmPolicyError("Hermes deployment must use a local IP URL") from error
        if (
            parsed.scheme != "http"
            or not isinstance(address, ipaddress.IPv4Address)
            or not address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or port is None
            or parsed.path.rstrip("/") != "/v1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise LiteLlmPolicyError("Hermes deployment must use a local IP URL")

    @staticmethod
    def render_empty() -> bytes:
        document = {
            "general_settings": {
                "database_url": "os.environ/LITELLM_DATABASE_URL",
                "disable_admin_ui": False,
                "master_key": "os.environ/LITELLM_MASTER_KEY",
                "store_model_in_db": False,
            },
            "litellm_settings": {
                "drop_params": True,
                "failure_callback": [],
                "set_verbose": False,
                "success_callback": [],
            },
            "model_list": [],
            "router_settings": {
                "enable_pre_call_checks": True,
                "routing_strategy": "simple-shuffle",
            },
        }
        return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()

    def publish(self, routes: RouteState, policy: LiteLlmPolicy) -> LiteLlmGeneration:
        content = self.render(routes, policy)
        return self._publish_content(content, routes.digest)

    def publish_empty(self, route_digest: str) -> LiteLlmGeneration:
        """Atomically withdraw every model while retaining generation history."""
        if re.fullmatch(r"[0-9a-f]{64}", route_digest) is None:
            raise LiteLlmPolicyError("empty LiteLLM route digest is invalid")
        return self._publish_content(self.render_empty(), route_digest)

    def _publish_content(
        self, content: bytes, route_digest: str
    ) -> LiteLlmGeneration:
        if self._validate(content) is not True:
            raise LiteLlmPolicyError("LiteLLM candidate failed validation")
        current = self.active(optional=True)
        number = (current.generation if current else 0) + 1
        digest = hashlib.sha256(content).hexdigest()
        directory = self._root / f"{number:08d}-{digest}"
        try:
            directory.mkdir(mode=0o700)
            target = directory / "config.yaml"
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content); output.flush(); os.fsync(output.fileno())
            self._apply(content)
        except LiteLlmPolicyError:
            raise
        except Exception as error:
            raise LiteLlmPolicyError("LiteLLM candidate apply failed; previous generation retained") from error
        generation = LiteLlmGeneration(number, route_digest, digest, str(target))
        pointer = (json.dumps(generation.__dict__, sort_keys=True, separators=(",", ":")) + "\n").encode()
        descriptor, temporary_raw = tempfile.mkstemp(prefix=".active-", dir=self._root)
        temporary = Path(temporary_raw)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(pointer); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, self._root / "active.json")
        finally:
            temporary.unlink(missing_ok=True)
        return generation

    def active(self, *, optional: bool = False) -> LiteLlmGeneration | None:
        pointer = self._root / "active.json"
        if not pointer.exists():
            if optional:
                return None
            raise LiteLlmPolicyError("no LiteLLM generation is active")
        if pointer.is_symlink() or not pointer.is_file():
            raise LiteLlmPolicyError("LiteLLM active pointer is unsafe")
        try:
            raw = json.loads(pointer.read_bytes())
            generation = LiteLlmGeneration(raw["generation"], raw["route_digest"], raw["config_sha256"], raw["path"])
            config = Path(generation.path)
            if config.is_symlink() or not config.is_file() or hashlib.sha256(config.read_bytes()).hexdigest() != generation.config_sha256:
                raise LiteLlmPolicyError("LiteLLM active generation checksum mismatch")
            return generation
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise LiteLlmPolicyError("LiteLLM active generation is unreadable") from error
