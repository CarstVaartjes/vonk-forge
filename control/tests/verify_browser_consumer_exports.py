"""Return actual browser exports to their owning Python readers without kind loss."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

from cross_language_consumer_corpus import MODELS


def assert_same_numeric_kinds(expected: object, actual: object, path: str) -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict) and expected.keys() == actual.keys(), path
        for key, value in expected.items():
            assert_same_numeric_kinds(value, actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(expected) == len(actual), path
        for index, (before, after) in enumerate(zip(expected, actual, strict=True)):
            assert_same_numeric_kinds(before, after, f"{path}[{index}]")
    elif type(expected) in (bool, int, float):
        assert type(actual) is type(expected), f"{path}: numeric kind changed"
        if type(expected) is float:
            assert isinstance(actual, float), path
            assert struct.pack(">d", expected) == struct.pack(">d", actual), (
                f"{path}: IEEE754 value or zero sign changed"
            )
        else:
            assert expected == actual, f"{path}: value changed"
    else:
        assert expected == actual, f"{path}: value changed"


def verify(corpus_path: Path, exports_path: Path) -> int:
    corpus = json.loads(corpus_path.read_text())
    output = json.loads(exports_path.read_text())
    assert corpus["version"] == output["version"] == 1
    cases = {
        case["id"]: case
        for case in corpus["cases"]
        if case["accepted"] and "browser" in case["consumers"]
    }
    seen = set()
    for exported in output["exports"]:
        identity = exported["id"]
        assert identity not in seen, f"duplicate export {identity}"
        seen.add(identity)
        case = cases[identity]
        model = MODELS[case["component"]]
        expected = model.model_validate_json(case["text"]).model_dump(mode="python")
        actual = model.model_validate_json(exported["text"]).model_dump(mode="python")
        assert_same_numeric_kinds(expected, actual, identity)
    assert seen == cases.keys(), f"missing browser exports: {cases.keys() - seen}"
    return len(seen)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--exports", type=Path, required=True)
    arguments = parser.parse_args()
    print(f"Verified {verify(arguments.corpus, arguments.exports)} browser roundtrips")
