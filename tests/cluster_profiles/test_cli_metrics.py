"""`vonkctl metrics` prints the catalogue's words and the shared number wording."""

import json
from pathlib import Path

import pytest

from cluster_profiles import cli
from cluster_profiles.cli_render.metrics import NO_DATA, metric_value_text, sparkline

_WORDING = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "metrics-wording.json").read_text()
)["cases"]
NODE = "spk_" + "1" * 32


class _Spec:
    def __init__(self, document):
        self.document = document

    def to_dict(self):
        return self.document


class _Client:
    """The catalogue call plus the two `request` reads the command makes."""

    request_timeout_seconds = 15.0

    def __init__(self, spec, series):
        self.spec, self.series, self.calls = spec, series, []

    def metrics_catalogue(self):
        return [_Spec(self.spec)]

    def request(self, method, path, payload=None, *, query=None, **_):
        self.calls.append((method, path, query))
        if path == "/api/fleet":
            return {"nodes": [{"id": NODE, "display_name": "Alpha"}]}
        return self.series


def _spec(**fields):
    return {
        "metric": "gpu_temperature",
        "title": "GPU temperature",
        "unit": "celsius",
        "scope": "node",
        "basis": "measured",
        "source": "spark",
        "summary_total": False,
        "fleet_sum": False,
        "hint": "GPU die temperature.",
        **fields,
    }


def _document(spec, series, range_="24h"):
    return {
        "metric": "gpu_temperature",
        "spec": spec,
        "range": range_,
        "start": 0.0,
        "end": 1.0,
        "step_seconds": 15,
        "series": series,
    }


def _series(labels, values, **summary):
    return {
        "labels": labels,
        "points": [{"timestamp": float(i), "value": v} for i, v in enumerate(values)],
        "summary": {
            "latest": None,
            "average": None,
            "peak": None,
            "total": None,
            "share": None,
            "coverage": 1,
            **summary,
        },
    }


def _run(argv, client, capsys):
    status = cli.main(argv, control_client=client, request_id_factory=lambda: "r" * 8)
    return status, capsys.readouterr().out


@pytest.mark.parametrize("case", _WORDING, ids=lambda c: f"{c['unit']}-{c['value']}")
def test_values_read_as_the_shared_wording(case):
    assert metric_value_text(case["unit"], case["value"]) == case["text"]


def test_sparkline_marks_gaps_and_flat_series():
    assert sparkline([1, 2, 3, 4, 5, 6, 7, 8]) == "▁▂▃▄▅▆▇█"
    assert sparkline([5] * 8) == "▄" * 8
    assert sparkline([1, None, None, None, None, None, None, 8]) == "▁      █"
    assert sparkline([]) == ""


def test_list_prints_the_catalogue(capsys):
    status, out = _run(("metrics",), _Client(_spec(), {}), capsys)
    assert status == 0
    assert "GPU temperature" in out and "celsius" in out and "measured" in out


def test_show_names_the_spark_and_marks_estimates(capsys):
    series = _document(
        _spec(basis="estimated"),
        [
            _series(
                {"node_id": NODE}, [30, 40, 50, 60], latest=60, average=45, peak=61
            ),
            _series({"node_id": "spk_" + "2" * 32}, [None], coverage=0),
        ],
    )
    client = _Client(_spec(), series)
    status, out = _run(("metrics", "show", "gpu_temperature"), client, capsys)
    assert status == 0
    assert client.calls[0] == (
        "GET",
        "/api/metrics/series",
        {"metric": "gpu_temperature", "range": "24h"},
    )
    assert [call[1] for call in client.calls].count("/api/fleet") == 1
    assert "GPU temperature, last 24 hours (estimate)" in out
    assert "Alpha" in out and "60 °C" in out and "(estimate)" in out
    assert NO_DATA in out


def test_show_json_passes_the_controller_document_through(capsys):
    series = _document(_spec(), [], "1h")
    status, out = _run(
        ("metrics", "show", "gpu_temperature", "--range", "1h", "--json"),
        _Client(_spec(), series),
        capsys,
    )
    assert status == 0
    assert json.loads(out)["series"] == [] and "schema_version" not in json.loads(out)
