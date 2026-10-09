import json

from cluster_profiles import cli
from cluster_profiles.control_client import _request_contract
from cluster_profiles.generated_control.models.gateway_key_create_request import (
    GatewayKeyCreateRequest,
)

SECRET = "sk-client-" + "s" * 32
REQUEST_ID = "10000000-0000-4000-8000-000000000001"


class KeyClient:
    request_timeout_seconds = 5.0

    def __init__(self):
        self.calls = []

    def request(self, method, path, payload=None, **kwargs):
        # Hold every call to the bundled OpenAPI contract, as the real client does.
        _request_contract(path, method, payload)
        self.calls.append((method, path, payload))
        if method == "GET":
            return {
                "keys": [
                    {
                        "name": "laptop",
                        "models": [],
                        "created_at": "2026-09-28T00:00:00+00:00",
                        "expires_at": None,
                        "last_used_at": None,
                    }
                ]
            }
        if path.endswith("/revoke"):
            return {"name": "laptop"}
        if path.endswith("/roll"):
            return {"name": "laptop", "models": [], "key": SECRET}
        assert payload is not None
        return {
            "name": payload["name"],
            "models": payload["models"],
            "created_at": "2026-09-28T00:00:00+00:00",
            "expires_at": None,
            "last_used_at": None,
            "key": SECRET,
        }


def test_key_create_prints_the_key_once(capsys):
    client = KeyClient()
    assert (
        cli.main(
            ["key", "create", "laptop"],
            control_client=client,
            request_id_factory=lambda: REQUEST_ID,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert output.count(SECRET) == 1
    assert "not shown again" in output
    assert client.calls == [
        (
            "POST",
            "/api/key",
            GatewayKeyCreateRequest(
                name="laptop", models=[], request_id=REQUEST_ID
            ).to_dict(),
        )
    ]


def test_key_create_output_writes_a_private_file(tmp_path, capsys):
    client = KeyClient()
    destination = tmp_path / "client-key"
    status = cli.main(
        [
            "--json",
            "key",
            "create",
            "ci",
            "--model",
            "qwen",
            "--expires",
            "30d",
            "--output",
            str(destination),
        ],
        control_client=client,
        request_id_factory=lambda: REQUEST_ID,
    )
    assert status == 0
    assert destination.read_text() == SECRET + "\n"
    assert destination.stat().st_mode & 0o777 == 0o600
    result = json.loads(capsys.readouterr().out)
    assert "key" not in result
    assert result["output"] == str(destination)
    assert (
        client.calls[0][2]
        == GatewayKeyCreateRequest(
            name="ci", models=["qwen"], expires="30d", request_id=REQUEST_ID
        ).to_dict()
    )


def test_key_list_and_revoke(capsys):
    client = KeyClient()
    assert cli.main(["key", "list"], control_client=client) == 0
    assert "laptop" in capsys.readouterr().out
    assert (
        cli.main(["key", "revoke", "laptop", "--no-input"], control_client=client) != 0
    )
    assert not any(call[0] == "POST" for call in client.calls)
    capsys.readouterr()
    assert cli.main(["key", "revoke", "laptop", "--yes"], control_client=client) == 0
    assert "Revoked key laptop." in capsys.readouterr().out
    assert client.calls[-1] == ("POST", "/api/key/laptop/revoke", None)


def test_key_roll_confirms_and_prints_the_new_key_once(capsys):
    client = KeyClient()
    assert cli.main(["key", "roll", "laptop", "--no-input"], control_client=client) != 0
    assert not any(call[0] == "POST" for call in client.calls)
    capsys.readouterr()
    assert cli.main(["key", "roll", "laptop", "--yes"], control_client=client) == 0
    assert capsys.readouterr().out.count(SECRET) == 1
    assert client.calls[-1] == ("POST", "/api/key/laptop/roll", None)
