from __future__ import annotations

from pathlib import Path

import pytest


class _ExecCalled(RuntimeError):
    pass


def test_privilege_drop_clears_groups_and_saved_root_ids_before_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vonk_control import api_preexec

    events: list[object] = []
    identity = {
        "groups": [0, 10001],
        "gid": (0, 0, 0),
        "uid": (0, 0, 0),
    }

    def setgroups(groups: list[int]) -> None:
        identity["groups"] = list(groups)
        events.append(("groups", tuple(groups)))

    def setresgid(real: int, effective: int, saved: int) -> None:
        identity["gid"] = (real, effective, saved)
        events.append(("gid", real, effective, saved))

    def setresuid(real: int, effective: int, saved: int) -> None:
        identity["uid"] = (real, effective, saved)
        events.append(("uid", real, effective, saved))

    def open_source_secrets(path: Path) -> None:
        events.append(("probe", path))
        if identity["uid"] != (10001, 10001, 10001):
            raise AssertionError("source secrets were probed before the UID drop")
        raise PermissionError

    def exec_process(command: tuple[str, ...]) -> None:
        events.append(("exec", command))
        raise _ExecCalled

    monkeypatch.setattr(api_preexec.os, "setgroups", setgroups)
    monkeypatch.setattr(api_preexec.os, "setresgid", setresgid)
    monkeypatch.setattr(api_preexec.os, "setresuid", setresuid)
    monkeypatch.setattr(api_preexec.os, "getgroups", lambda: identity["groups"])
    monkeypatch.setattr(api_preexec.os, "getresgid", lambda: identity["gid"])
    monkeypatch.setattr(api_preexec.os, "getresuid", lambda: identity["uid"])

    with pytest.raises(_ExecCalled):
        api_preexec.drop_privileges_and_exec(
            ("python", "-m", "vonk_control.api"),
            source_secrets=Path("/run/secrets"),
            source_probe=open_source_secrets,
            execute=exec_process,
        )

    assert events == [
        ("groups", ()),
        ("gid", 10001, 10001, 10001),
        ("uid", 10001, 10001, 10001),
        ("probe", Path("/run/secrets")),
        ("exec", ("python", "-m", "vonk_control.api")),
    ]


def test_preexec_initializes_owned_state_before_dropping_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vonk_control import api_preexec

    events: list[str] = []
    monkeypatch.setattr(
        api_preexec,
        "prepare_owned_state",
        lambda: events.append("prepare"),
    )
    monkeypatch.setattr(
        api_preexec,
        "drop_privileges_and_exec",
        lambda command: events.append("exec:" + " ".join(command)),
    )

    api_preexec.main(("python", "-m", "vonk_control.api"))

    assert events == ["prepare", "exec:python -m vonk_control.api"]
