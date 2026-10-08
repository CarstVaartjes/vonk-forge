"""Coherent module inventories with bounded observation and crash reconciliation.

The manifest defines completeness. A durable publication journal preserves the
last committed view until atomic manifest replacement. Snapshot-derived edits
are fenced by module content, while explicit replacement repairs unreadable
local storage. Consumer validation belongs to the same observation budget.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from tools.registry_contracts import RegistryManifest, RegistryPublication


def _source(value):
    return isinstance(value, str) and "/" in value and not value.startswith("/")


def _merge(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        result = dict(left)
        for key, value in right.items():
            result[key] = _merge(result[key], value) if key in result else value
        return result
    if isinstance(left, list) and isinstance(right, list):
        result = list(left)
        for value in right:
            if isinstance(value, dict) and "family" in value:
                match = next(
                    (
                        i
                        for i, item in enumerate(result)
                        if isinstance(item, dict)
                        and item.get("family") == value["family"]
                    ),
                    None,
                )
                if match is not None:
                    result[match] = _merge(result[match], value)
                    continue
            result.append(value)
        return result
    if left != right:
        raise ValueError("registry shards have conflicting metadata")
    return left


def _split(value):
    if isinstance(value, dict):
        owner = next(
            (
                value[key]
                for key in ("path", "file", "module")
                if key in value and _source(value[key])
            ),
            None,
        )
        if owner is not None:
            return {owner: value}
        shards = {"_global": {}}
        for key, child in value.items():
            pieces = (
                {"_global": child}
                if key.endswith("_roots")
                else {key: child}
                if _source(key)
                else _split(child)
            )
            for owner, piece in pieces.items():
                shards.setdefault(owner, {})[key] = piece
        return shards
    if isinstance(value, list):
        shards = {"_global": []}
        for child in value:
            if _source(child):
                pieces = {child: child}
            elif isinstance(child, list) and child and _source(child[0]):
                pieces = {child[0]: child}
            elif isinstance(child, dict) and "family" in child:
                pieces = _split(child.get("sites", []))
                pieces = {
                    owner: (
                        {**child, "sites": sites}
                        if owner == "_global"
                        else {"family": child["family"], "sites": sites}
                    )
                    for owner, sites in pieces.items()
                }
            else:
                pieces = _split(child)
            for owner, piece in pieces.items():
                shards.setdefault(owner, []).append(piece)
        return shards
    return {"_global": value}


def _sorted(value):
    if isinstance(value, dict):
        return {key: _sorted(child) for key, child in sorted(value.items())}
    if isinstance(value, list):
        items = [_sorted(child) for child in value]
        # Raise-site rows are positional; all inventory collections are sets.
        if value and _source(value[0]) and any(not _source(v) for v in value[1:]):
            return items
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    return value


def _derive(document):
    if "fail_closed" in document:
        audited = set(document["scope"]["audited_paths"])
        debt = {"total": 0, "unaudited": 0}
        for family in document["fail_closed"]:
            if family["category"] == "bookkeeping-debt":
                for site in family["sites"]:
                    debt["total" if site[0] in audited else "unaudited"] += site[4]
        document["debt_ceiling"].update(debt)
        document["max_debt"] = sum(
            entry["verdict"] != "KEEP" for entry in document["operator_waits"]
        )
        section = document["categorized_raises"]
        section["ceiling"] = sum(section["grandfathered"].values())
    return document


class RegistryObservationUnavailable(RuntimeError):
    """A bounded inventory observation ended without a complete document."""


class RegistrySnapshot(dict):
    """An observed document retaining the module bytes used to author changes."""

    def __init__(
        self, document: dict, fragments: dict[str, str], owner: Path | None = None
    ):
        super().__init__(document)
        self.fragments = dict(fragments)
        self.owner = owner


def _manifest(fragments: dict[str, str]) -> RegistryManifest:
    return RegistryManifest(
        shards={
            name: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for name, text in fragments.items()
        }
    )


def _assemble(fragments: dict[str, str], owner: Path | None = None) -> RegistrySnapshot:
    document = {}
    for name, text in sorted(fragments.items()):
        fragment = json.loads(text)
        if not isinstance(fragment, dict):
            raise TypeError(f"{name}: registry fragment is not an object")
        document = _merge(document, fragment)
    return RegistrySnapshot(cast(dict, _derive(_sorted(document))), fragments, owner)


def _read_once(path: Path) -> RegistrySnapshot:
    try:
        pending = RegistryPublication.model_validate_json(
            (path / ".publication").read_bytes()
        )
    except FileNotFoundError:
        pending = None
    if pending is not None:
        # A killed publisher leaves the last complete view in its durable
        # journal. The manifest is the single visibility/commit point.
        try:
            current = RegistryManifest.model_validate_json(
                (path / ".manifest").read_bytes()
            )
        except FileNotFoundError:
            current = None
        if current == pending.desired_manifest:
            return _assemble(pending.desired, path.resolve())
        if current != pending.previous_manifest or current is None:
            raise ValueError("registry publication has no complete committed view")
        return _assemble(pending.previous, path.resolve())
    first = (path / ".manifest").read_bytes()
    manifest = RegistryManifest.model_validate_json(first)
    names = {p.relative_to(path).as_posix() for p in path.rglob("*.json")}
    if names != set(manifest.shards):
        raise ValueError("registry shard inventory is incomplete")
    fragments = {
        name: (path / name).read_text(encoding="utf-8") for name in manifest.shards
    }
    if _manifest(fragments) != manifest or (path / ".manifest").read_bytes() != first:
        raise ValueError("registry changed during observation")
    return _assemble(fragments, path.resolve())


def read_registry(path: Path) -> RegistrySnapshot:
    cause: Exception | None = None
    for attempt in range(2):
        try:
            return _read_once(path)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
        ) as error:
            cause = error
            if attempt == 0:
                time.sleep(0.01)
    raise RegistryObservationUnavailable(
        f"{path}: inventory unavailable after two observations: {cause}"
    ) from cause


def observe_registry[Observed](
    path: Path, validate: Callable[[dict, Path], Observed]
) -> Observed:
    """Include the consumer's document decoding in the bounded observation."""
    cause: Exception | None = None
    for attempt in range(2):
        try:
            snapshot = _read_once(path)
            result = validate(snapshot, path)
            if isinstance(result, dict):
                return cast(
                    Observed,
                    RegistrySnapshot(result, snapshot.fragments, snapshot.owner),
                )
            return result
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
        ) as error:
            cause = error
            if attempt == 0:
                time.sleep(0.01)
    raise RegistryObservationUnavailable(
        f"{path}: inventory unavailable after two observations: {cause}"
    ) from cause


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".registry-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _publish(path: Path, pending: RegistryPublication) -> None:
    for name, text in pending.desired.items():
        target = path / name
        try:
            unchanged = target.read_bytes() == text.encode("utf-8")
        except OSError:
            unchanged = False
        if not unchanged:
            _atomic_write(target, text.encode("utf-8"))
    for target in path.rglob("*.json"):
        if target.relative_to(path).as_posix() not in pending.desired:
            target.unlink(missing_ok=True)
    _atomic_write(
        path / ".manifest", pending.desired_manifest.model_dump_json().encode()
    )
    (path / ".publication").unlink(missing_ok=True)
    # Crash leftovers are unpublished temporary bytes, never inventory entries.
    for temporary in path.rglob(".registry-*"):
        temporary.unlink(missing_ok=True)


def _render(document: dict) -> dict[str, str]:
    document = json.loads(json.dumps(document))
    if "fail_closed" in document:
        document.pop("max_debt", None)
        document["debt_ceiling"].pop("total", None)
        document["debt_ceiling"].pop("unaudited", None)
        document["categorized_raises"].pop("ceiling", None)
    expected = {
        owner + ".json": json.dumps(_sorted(fragment), indent=2, sort_keys=True) + "\n"
        for owner, fragment in _split(document).items()
    }
    # Validate explicit replacement bytes and path ownership before effects.
    _manifest(expected)
    _assemble(expected)
    return expected


def write_registry(
    path: Path, document: dict, *, base: RegistrySnapshot | None = None
) -> None:
    expected = _render(document)
    if (
        base is None
        and isinstance(document, RegistrySnapshot)
        and document.owner == path.resolve()
    ):
        base = document
    cause: Exception | None = None
    for attempt in range(2):
        try:
            path.mkdir(parents=True, exist_ok=True)
            with (path / ".writer-lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Finish an accepted interrupted publication before considering
                # a subsequent request. Kernel ownership ends on process death.
                try:
                    pending = RegistryPublication.model_validate_json(
                        (path / ".publication").read_bytes()
                    )
                except FileNotFoundError:
                    pending = None
                if pending is not None:
                    _publish(path, pending)
                try:
                    current = _read_once(path)
                    previous = current.fragments
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    IndexError,
                    AttributeError,
                ):
                    # An explicit, complete replacement can repair damaged local
                    # observation. Never require reading a corrupt target first.
                    if base is not None:
                        raise
                    previous = {}
                desired = dict(expected)
                if base is not None:
                    desired = dict(previous)
                    for name in base.fragments.keys() | expected.keys():
                        old, new = base.fragments.get(name), expected.get(name)
                        if old == new:
                            continue
                        if previous.get(name) not in (old, new):
                            raise ValueError(
                                f"{name}: newer module bytes supersede this snapshot"
                            )
                        if new is None:
                            desired.pop(name, None)
                        else:
                            desired[name] = new
                if desired == previous:
                    return
                pending = RegistryPublication(
                    previous=previous,
                    desired=desired,
                    previous_manifest=_manifest(previous) if previous else None,
                    desired_manifest=_manifest(desired),
                )
                _assemble(desired)
                _atomic_write(path / ".publication", pending.model_dump_json().encode())
                _publish(path, pending)
                return
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
        ) as error:
            cause = error
            if attempt == 0:
                time.sleep(0.01)
    raise RegistryObservationUnavailable(
        f"{path}: publication unavailable after two attempts: {cause}"
    ) from cause
