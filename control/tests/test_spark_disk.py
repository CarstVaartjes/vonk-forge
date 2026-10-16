"""Admission judges a Spark's disk as free plus what its agent frees itself.

A Spark's agent removes its unused model files before a claim writes, so
every admission decision counts them (``spark_free_bytes``). A decision that
read the raw free disk would refuse work the Spark can take, while the
storage collector, counting them, removes nothing for it: the work would
wait for ever.
"""

from __future__ import annotations

import ast
from pathlib import Path

from vonk_control.spark_disk import spark_free_bytes

SOURCE = Path(__file__).resolve().parents[1] / "src/vonk_control"

#: Every module that admits work on a Spark's free disk.
ADMISSION = (
    "install_admission/planning.py",
    "run_switch_operations/resource_fit.py",
    "recipe_builds/planning.py",
    "recipe_builds/persistence.py",
)


def test_free_disk_counts_unused_model_files_less_what_the_claim_reuses() -> None:
    assert spark_free_bytes(100, None) == 100
    assert spark_free_bytes(100, 50) == 150
    # The agent never removes a claim's own objects.
    assert spark_free_bytes(100, 50, reused_model_bytes=20) == 130
    assert spark_free_bytes(100, 50, reused_model_bytes=80) == 100


def test_admission_reads_free_disk_only_through_spark_free_bytes() -> None:
    raw = []
    for name in ADMISSION:
        tree = ast.parse((SOURCE / name).read_text())
        wrapped = {
            id(argument)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == spark_free_bytes.__name__
            for argument in node.args
        }
        raw.extend(
            f"{name}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == "disk_free_bytes"
            and isinstance(node.ctx, ast.Load)
            and id(node) not in wrapped
        )
    assert raw == [], f"read free disk through spark_free_bytes: {raw}"
