"""`vonkctl metrics`: the series request, the Spark names it adds, and sparklines."""

from cluster_profiles.cli_render.metrics import sparkline

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


def test_sparkline_marks_gaps_and_flat_series():
    assert sparkline([1, 2, 3, 4, 5, 6, 7, 8]) == "▁▂▃▄▅▆▇█"
    assert sparkline([5] * 8) == "▄" * 8
    assert sparkline([1, None, None, None, None, None, None, 8]) == "▁      █"
    assert sparkline([]) == ""


def test_show_reads_the_series_then_names_its_sparks_once(vonkctl_json):
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
    status, document = vonkctl_json(
        "metrics", "show", "gpu_temperature", control_client=client
    )
    assert status == 0
    assert client.calls[0] == (
        "GET",
        "/api/metrics/series",
        {"metric": "gpu_temperature", "range": "24h"},
    )
    assert [call[1] for call in client.calls].count("/api/fleet") == 1
    assert document["names"] == {NODE: "Alpha"}


def test_show_of_an_empty_series_reads_no_roster_and_is_no_contract_document(
    vonkctl_json,
):
    client = _Client(_spec(), _document(_spec(), [], "1h"))
    status, document = vonkctl_json(
        "metrics", "show", "gpu_temperature", "--range", "1h", control_client=client
    )
    assert status == 0
    assert client.calls == [
        ("GET", "/api/metrics/series", {"metric": "gpu_temperature", "range": "1h"})
    ]
    assert document["names"] == {}
    assert "schema_version" not in document
