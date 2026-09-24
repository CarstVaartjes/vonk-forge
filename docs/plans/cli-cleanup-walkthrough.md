# U8 disposable cleanup walkthrough

This facilitator covers the cleanup part of U8 with an independently built
`vonkctl` wheel, a registered loopback HTTPS Controller, disposable PostgreSQL,
and managed model/image storage. The participant receives only the shipped
[`vonkctl` runbook](../runbooks/vonkctl.md) and the original U8 outcome card.
Do not provide this guide, the test, source, expected outcomes, or a command
sequence. A shell session is facilitator evidence; it does not fill in the
independent human scorecard or close W19.

Run the smoke from the repository root with OrbStack available for the
disposable PostgreSQL fixture:

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes
export UV_CACHE_DIR=/private/tmp/vonk-forge-cli-implementation-cache

VONK_U8_CLEANUP_MODE=smoke PYTHONPATH="$PWD:$PWD/src:$PWD/control/src" \
  uv run --project control --frozen --with-editable . \
  pytest -p control.tests.required_execution -q -s \
  control/tests/test_cli_cleanup_walkthrough.py::test_disposable_u8_cleanup_walkthrough_smoke

VONK_U8_CLEANUP_MODE=interactive PYTHONPATH="$PWD:$PWD/src:$PWD/control/src" \
  uv run --project control --frozen --with-editable . \
  pytest -p control.tests.required_execution -q -s \
  control/tests/test_cli_cleanup_walkthrough.py::test_disposable_u8_cleanup_walkthrough_interactive
```

The second command opens the human shell. Give the participant the original U8
card verbatim. The mode and selected node must match; changing only the mode
causes the required-execution check to fail.
The shell has a private `HOME`, a mode-0600 token file, no history file, and the
exact installed wheel first on `PATH`. Its Controller URL is loopback HTTPS.
Type `exit` or press Ctrl-D to close the shell and let pytest remove its wheel,
TLS material, token, and managed files and dispose of the fixture database.

The smoke asks the installed CLI for an exact recipe-removal review. It checks
that the review shows the selected recipe revision, runtime-image archive,
selected model-cache set, shared model object disposition and surviving set
reference before any mutation. A scripted attempt without explicit `--yes`
must leave PostgreSQL, model bytes, image bytes, and HTTP mutation counts
unchanged. The accepted request binds the exact review digest; its initial
receipt and progress remain queued until the real removal owners run. The test
then advances the registered recipe and model-cache owners and checks the
terminal progress against managed storage: the selected image and model set
are removed, while the sibling set, its shared verified object and receipt, and
the current recipe catalog head remain. A queued receipt is never counted as
completed cleanup.

The interactive fixture starts no removal worker. While the shell is open, an
accepted request remains queued and the participant can observe that accepted
state; the facilitator advances the image and model-cache owners only after the
shell exits. This session therefore cannot show the participant terminal
cleanup progress. The smoke mode supplies that separate completion evidence.

The fixture seeds only local, verified test bytes and uses the real registered
API, cache owners, review projection, durable request, and removal workers. It
does not fetch an external artifact, deploy a Controller, contact a Spark, or
prove model/runtime execution. The interactive shell starts no automatic
removal worker; if an operator accepts the fixture action, the facilitator
checks that recorded scope before settling the disposable owners. If no request
is accepted, no cleanup is run. This facilitator performs no live cleanup and
does not authorize cleanup outside its disposable fixture. Check the current
W09/W17 and [W19 package row](cli-operator-status.md#package-state) before
scheduling any separate participant session;
those gates and the human scorecard remain independent.
