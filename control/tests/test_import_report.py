import pytest
from vonk_control.import_report import (
    ImportDisposition,
    ImportReportBuilder,
    ImportReportError,
)


def test_report_requires_one_terminal_disposition_per_source_leaf() -> None:
    report = ImportReportBuilder(("/a", "/b"))
    report.record(
        "/a",
        ImportDisposition.IMPORTED,
        "/metadata/title",
        "metadata.title",
        "Title imported",
        False,
    )

    with pytest.raises(ImportReportError):
        report.finalize()

    report.record(
        "/b",
        ImportDisposition.DROPPED_REDUNDANT,
        None,
        "schema.redundant",
        "Already represented",
        False,
    )
    items = report.finalize()
    assert [item.source_path for item in items] == ["/a", "/b"]


def test_duplicate_source_or_destination_is_rejected() -> None:
    report = ImportReportBuilder(("/a", "/b"))
    report.record(
        "/a",
        ImportDisposition.IMPORTED,
        "/identity/slug",
        "identity",
        "Imported",
        False,
    )
    with pytest.raises(ImportReportError):
        report.record(
            "/a", ImportDisposition.IMPORTED, "/other", "duplicate", "Duplicate", False
        )
    with pytest.raises(ImportReportError):
        report.record(
            "/b",
            ImportDisposition.IMPORTED,
            "/identity/slug",
            "duplicate",
            "Duplicate",
            False,
        )
