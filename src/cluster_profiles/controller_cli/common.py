"""CLI request protocol, argument validation, and shared parser controls."""

from __future__ import annotations

import argparse
import math
import re
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..generated_control.models.fleet_profile_endpoints_view import (
        FleetProfileEndpointsView,
    )


MAX_PAGE_LIMIT = 512


MAX_LOG_LINES = 1000


class ControllerClient(Protocol):
    @property
    def request_timeout_seconds(self) -> float: ...

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]: ...

    def profile_endpoints(
        self, number: int, alias: str | None = None
    ) -> FleetProfileEndpointsView: ...


@runtime_checkable
class _WatchCallback(Protocol):
    """The progress callback the terminal renderer stores on the namespace."""

    def __call__(self, observed: Mapping[str, object]) -> None: ...


def _quoted(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _log_since(value: str) -> str:
    relative = re.fullmatch(r"([0-9]+)([smhd])", value)
    try:
        if relative is not None:
            seconds = (
                int(relative[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[relative[2]]
            )
            return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is not None:
            return value
    except (ValueError, OverflowError):
        pass
    raise ValueError(
        "--since requires a duration such as 15m or a timestamp with a timezone"
    )


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print one JSON document instead of text",
    )
    parser.add_argument(
        "--wide",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Show every field and identifier",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Never prompt; changes then need --yes",
    )


class _ValueList(argparse.Action):
    """A multi-value flag: repeat it, comma-separate values, or mix both.

    ``--engine vllm,sglang`` equals ``--engine vllm --engine sglang``. Values
    are trimmed, empties dropped, and duplicates removed in first-seen order.
    Only use it where a value can never contain a comma.
    """

    def __init__(self, *args: object, item: Callable[[str], object] = str, **kwargs):
        self._item = item
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[no-untyped-def]
        current = list(getattr(namespace, self.dest, None) or [])
        for raw in str(values).split(","):
            text = raw.strip()
            if not text:
                continue
            try:
                value = self._item(text)
            except (ValueError, argparse.ArgumentTypeError):
                raise argparse.ArgumentError(self, f"invalid value {text!r}") from None
            if value not in current:
                current.append(value)
        setattr(namespace, self.dest, current)


def _value_list(
    parser: argparse.ArgumentParser,
    flag: str,
    metavar: str,
    help: str,
    *,
    item: Callable[[str], object] = str,
    dest: str | None = None,
) -> None:
    """Add a comma-or-repeat filter flag with the shared parsing."""

    parser.add_argument(
        flag,
        action=_ValueList,
        item=item,
        default=[],
        metavar=f"{metavar}[,{metavar}\u2026]",
        help=f"{help}; comma-separated or repeated",
        dest=dest,
    )


def _query(**values: object) -> dict[str, object]:
    return {
        key: value for key, value in values.items() if value not in (None, "", [], ())
    }


def _request_key(args: argparse.Namespace, factory: Callable[[], str]) -> str:
    """Resolve and retain the logical request key for this invocation.

    The key has to exist before the mutation is submitted and stay on the
    parsed arguments afterwards.  When the Controller accepts a submission and
    the response is lost, the error path can only name the durable operation by
    a key it can still read, and a following manual retry has to reconcile that
    same request instead of submitting the same intent under a fresh one.
    """

    value = getattr(args, "request_key", None) or factory()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("--request-key must be a UUID") from None
    resolved = str(parsed)
    args.request_key = resolved
    return resolved


def _bounded_int(value: str, *, label: str, minimum: int, maximum: int) -> int:
    """Accept an integer within a closed range.

    The bound belongs in the conversion rather than in ``choices``: a range of
    choices turns every ``--help`` that lists the option into a wall of numbers
    that hides the real options.
    """

    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid {label}: {value!r} is not an integer"
        ) from None
    if not minimum <= number <= maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return number


def _page_limit(value: str) -> int:
    """Accept a page size between 1 and 512."""

    return _bounded_int(value, label="page limit", minimum=1, maximum=MAX_PAGE_LIMIT)


def _line_limit(value: str) -> int:
    """Accept a log tail line count between 1 and 1000."""

    return _bounded_int(value, label="line count", minimum=1, maximum=MAX_LOG_LINES)


def _sha256_digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError(
            "review digest must be the complete lowercase SHA-256 value"
        )
    return value


def _activity_limit(value: str) -> int:
    """Accept the Controller's bounded Activity page size."""

    return _bounded_int(value, label="activity page size", minimum=1, maximum=100)


def _activity_state(value: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value) is None:
        raise argparse.ArgumentTypeError("activity state is invalid")
    return value


def _activity_target(value: str) -> str:
    if re.fullmatch(r"spk_[0-9a-f]{32}", value) is None:
        raise argparse.ArgumentTypeError("target must be an exact Spark ID")
    return value


def _activity_cursor(value: str) -> str:
    if not value or len(value) > 512:
        raise argparse.ArgumentTypeError(
            "activity cursor must contain 1 to 512 characters"
        )
    return value


def _selector(parser: argparse.ArgumentParser, metavar: str, *, help: str) -> None:
    parser.add_argument("selector", metavar=metavar.upper(), help=help)


def _filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--search", default="")
    for name in (
        "usage",
        "family",
        "version",
        "quantization",
        "publisher",
        "alignment",
    ):
        _value_list(parser, f"--{name}", name, f"Only this {name}")
    parser.add_argument("--updated-since")
    parser.add_argument(
        "--cached", action="store_true", help="Only what is already in the cache"
    )
    parser.add_argument("--sort", choices=("updated", "name"), default="updated")
    parser.add_argument("--cursor")
    parser.add_argument(
        "--limit",
        type=_page_limit,
        default=100,
        metavar="1-512",
        help=f"Page size, 1 to {MAX_PAGE_LIMIT} (default: 100)",
    )


def _watch_controls(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout-seconds",
        type=_timeout_seconds,
        default=30,
        metavar="SECONDS",
        help="Stop watching after this long, 0–300 (default: 30); the work continues",
    )
    parser.add_argument(
        "--interval-seconds",
        type=_interval_seconds,
        default=1.0,
        metavar="SECONDS",
        help="Time between refreshes, 0.01–30 (default: 1)",
    )


def _selection_controls(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout-seconds",
        type=_timeout_seconds,
        default=30,
        metavar="SECONDS",
        help="Give up choosing after this long, 0–300 (default: 30)",
    )


def _finite_seconds(value: str, *, label: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{label} requires a number of seconds"
        ) from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be {minimum:g}–{maximum:g} seconds; received {value}"
        )
    return number


def _timeout_seconds(value: str) -> float:
    return _finite_seconds(value, label="observation timeout", minimum=0, maximum=300)


def _interval_seconds(value: str) -> float:
    return _finite_seconds(value, label="poll interval", minimum=0.01, maximum=30)


def _add_yes(parser: argparse.ArgumentParser) -> None:
    """Every mutating command accepts --yes; it only matters where one asks first."""
    parser.add_argument("--yes", action="store_true", help="Confirm without asking")


def _action_flags(
    parser: argparse.ArgumentParser,
    *,
    recipe_remove: bool = False,
    followable: bool = False,
) -> None:
    parser.set_defaults(outcome_context="mutation")
    parser.add_argument(
        "--request-key",
        help="Original request UUID; supply and retain it to reconnect after process death",
    )
    if followable:
        parser.add_argument(
            "--detach",
            action="store_true",
            help="Return once accepted instead of following",
        )
        _watch_controls(parser)
    _add_yes(parser)
    if recipe_remove:
        model_choice = parser.add_mutually_exclusive_group()
        model_choice.add_argument("--with-model", action="store_true")
        model_choice.add_argument("--keep-model", action="store_true")
    _add_output(parser)


def _profile_edit_flags(
    parser: argparse.ArgumentParser, *, require_revision: bool = False
) -> None:
    parser.set_defaults(outcome_context="mutation", requires_profile=True)
    parser.add_argument(
        "--expected-revision",
        type=_revision,
        required=require_revision,
        metavar="N",
        help="Refuse the edit unless the saved profile is at revision N",
    )
    _add_yes(parser)
    _add_output(parser)


def _revision(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "revision must be a nonnegative integer"
        ) from None
    if number < 0:
        raise argparse.ArgumentTypeError(
            "revision must be nonnegative; zero means create only"
        )
    return number


def _uuid_argument(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("grant identity must be a UUID4") from None
    if parsed.version != 4 or str(parsed) != value:
        raise argparse.ArgumentTypeError("grant identity must be a canonical UUID4")
    return value


def _add_profile_selection(parser: argparse.ArgumentParser) -> None:
    """Accept ``--profile N`` after the command, as well as before it.

    The suppressed default keeps a value given before the command from being
    overwritten when the flag is not repeated here.
    """

    parser.add_argument(
        "--profile",
        dest="profile_number",
        type=int,
        default=argparse.SUPPRESS,
        metavar="N",
        help="Profile number (default: 1; required to change a profile)"
        if parser.get_default("requires_profile")
        else "Profile number (default: 1)",
    )


def _option_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Recipe option choice; repeatable. Options left out use the recipe default",
    )


def _uuid_selector(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError("operation selector must be a UUID") from None


def _profile_number(args: argparse.Namespace) -> int:
    number = getattr(args, "profile_number", None)
    if number is None:
        return 1
    if type(number) is not int or number < 1:
        raise ValueError("--profile must be a positive stable profile number")
    return number


__all__ = ["_WatchCallback"]
