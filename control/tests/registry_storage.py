"""Module-local storage for reviewed static inventories (no checkout history).

Shards retain the registry's existing document shape. Global metadata owns empty
containers and family descriptions; source paths own their entries. Policy and
validation remain with the consuming scanner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast


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
    return right


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


def read_registry(path: Path) -> dict:
    document = {}
    for shard in sorted(path.rglob("*.json")):
        document = _merge(document, json.loads(shard.read_text(encoding="utf-8")))
    return cast(dict, _derive(_sorted(document)))


def write_registry(path: Path, document: dict) -> None:
    # Serialize before effects. Source paths are supplied by the scanners, never
    # by remote callers. Only files inside this registry are reconciled.
    document = json.loads(json.dumps(document))
    if "fail_closed" in document:
        document.pop("max_debt")
        document["debt_ceiling"].pop("total")
        document["debt_ceiling"].pop("unaudited", None)
        document["categorized_raises"].pop("ceiling")
    expected = {}
    for owner, fragment in _split(document).items():
        target = path / (owner + ".json")
        expected[target] = (
            json.dumps(_sorted(fragment), indent=2, sort_keys=True) + "\n"
        )
    for target, text in expected.items():
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    for target in path.rglob("*.json"):
        if target not in expected:
            target.unlink()
