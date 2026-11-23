"""The shared lifecycle core of the Controller.

Composition, not inheritance: one pure transition function
(:func:`transition`), one adapter protocol per kind (:class:`KindAdapter`), and
one reconcile loop (:class:`Reconciler`).  The design and the migration order are
in the blocker audit (section 5); every lifecycle writer now lives in this
package, and ``control/tests/lifecycle_writer_boundaries.py`` fails any state
write outside it (no allowlist)."""
