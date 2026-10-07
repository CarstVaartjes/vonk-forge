"""Canonical activity leaves do not declare a retained lifecycle state."""

import ast

from .vocabulary_literals import LEGACY_STATE_WORDS, _state_literals


def test_activity_literal_requires_unshadowed_canonical_field_owner():
    imported = "from vonk_agent_protocol import OperationMemberProgress\n"
    expression = 'value = OperationMemberProgress(state=state, activity="waiting" if state == LifecycleState.QUEUED else None)\n'

    def found(source):
        return [
            node.value
            for node in _state_literals(ast.parse(source), set(), LEGACY_STATE_WORDS)
        ]

    assert found(imported + expression) == []
    assert (
        found(
            imported
            + 'value = OperationMemberProgress(state=state, activity="waiting")\n'
        )
        == []
    )
    assert found(imported + expression.replace("state=state", 'state="waiting"')) == [
        "waiting"
    ]
    assert found(
        imported + expression.replace("LifecycleState.QUEUED", '"waiting"')
    ) == ["waiting"]
    assert found(imported.replace("vonk_agent_protocol", "unrelated") + expression) == [
        "waiting"
    ]
    for binding in (
        "OperationMemberProgress = unrelated\n",
        "class OperationMemberProgress: pass\n",
        "from unrelated import OperationMemberProgress\n",
        "from unrelated import *\n",
    ):
        assert found(imported + binding + expression) == ["waiting"]
    assert found(
        imported + "def f(OperationMemberProgress, state):\n    " + expression
    ) == ["waiting"]
