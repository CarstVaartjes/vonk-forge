"""The state words the CLI recognises, in the core vocabulary.

The CLI ships on its own and does not import the Controller's contract package, so
this is its one copy of the words (a test keeps it equal to the contract).  The
Controller names a wait for a person ``needs-operator``; a Controller older than the
rename still sends ``waiting-for-operator``, which the CLI accepts for one release.
"""

from __future__ import annotations

NEEDS_OPERATOR = "needs-operator"
LEGACY_NEEDS_OPERATOR = "waiting-for-operator"
#: A wait for a person, under either spelling.
OPERATOR_WAIT_STATES = frozenset({NEEDS_OPERATOR, LEGACY_NEEDS_OPERATOR})
