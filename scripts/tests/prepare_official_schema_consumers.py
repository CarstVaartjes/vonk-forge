#!/usr/bin/env python3
"""Prepare official mathematical-schema cases for the actual browser compiler.

This selects pinned upstream tests, not a replacement schema or JSON parser.
Wire lexeme restrictions are exercised separately by the consumer corpus.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

PIN = "5b0ee1613e45fcc2bddac00e07c19cd49b00d8a8"
FILES = (
    "type",
    "required",
    "properties",
    "additionalProperties",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "enum",
    "const",
    "allOf",
    "anyOf",
    "oneOf",
)


class NumberToken(str):
    """Fixture-only numeric spelling; production parser still consumes the raw text."""


def raw_json(value) -> str:
    if isinstance(value, NumberToken):
        return str(value)
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                json.dumps(key) + ":" + raw_json(item) for key, item in value.items()
            )
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(raw_json(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def prepare(repository: Path, suite: Path) -> None:
    actual = subprocess.check_output(
        ["git", "-C", str(suite), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != PIN:
        raise SystemExit(f"official schema suite must be pinned at {PIN}, got {actual}")
    api = repository / "control/web/src/api"
    imports = [
        'import {test, expect} from "vitest";',
        'import {parseContractJson} from "./contract-numeric";',
    ]
    tests = []
    groups = 0
    total = 0
    for name in FILES:
        source = suite / "tests/draft2020-12" / f"{name}.json"
        # This JSON describes test strings/schemas. Consumer documents themselves
        # are passed as raw JSON strings to the production lossless parser.
        for group in json.loads(
            source.read_text(), parse_int=NumberToken, parse_float=NumberToken
        ):
            number = groups
            groups += 1
            schema_path = api / f"official-suite-{number}.schema.json"
            validator_path = api / f"official-suite-{number}.generated.js"
            schema_path.write_text(raw_json(group["schema"]) + "\n")
            subprocess.run(
                [
                    "node",
                    "tools/openapi-client/generate-runtime.mjs",
                    "--schema",
                    str(schema_path),
                    str(validator_path),
                ],
                cwd=repository,
                check=True,
                timeout=30,
            )
            imports.append(
                f'import {{validateSchema as validate{number}}} from "./{validator_path.name}";'
            )
            for case in group["tests"]:
                total += 1
                title = f"{name}: {group['description']}: {case['description']}"
                text = raw_json(case["data"])
                tests.append(
                    f"test({json.dumps(title)}, () => expect(validate{number}(parseContractJson({json.dumps(text)}))).toBe({str(case['valid']).lower()}));"
                )
    (api / "official-schema.generated.test.ts").write_text(
        "\n".join(imports + tests) + "\n"
    )
    print(
        json.dumps(
            {
                "suite_sha": PIN,
                "draft": "2020-12",
                "files": FILES,
                "groups": groups,
                "tests": total,
                "semantics": "mathematical integers/exact decimals; strict wire tokens tested separately",
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument(
        "--repository", type=Path, default=Path(__file__).resolve().parents[2]
    )
    args = parser.parse_args()
    prepare(args.repository.resolve(), args.suite.resolve())
