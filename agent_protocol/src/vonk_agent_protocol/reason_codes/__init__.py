"""Closed reason, blocker, warning and attention codes shared by Python, Rust and TypeScript.

Every code the Controller shows an operator as *why* something is waiting, refused,
blocked or degraded is a member of one of these enums, grouped by the domain that
raises it.  ``scripts/dev/export-agent-wire-schema`` publishes them through
:class:`ReasonCodeVocabulary`, ``scripts/dev/generate-agent-wire`` turns that into Rust
types and ``scripts/dev/generate-control-clients`` into OpenAPI and TypeScript, so no
consumer spells a code by hand.  The vocabulary ratchet
(``control/tests/vocabulary_literals.py``) fails a string literal that equals a
member and a string constant in any code position of the Controller.

The values are the spellings already stored in rows and shown by the API: this
module adds no new word, it only closes the set.  The exceptions are the agent's
runtime preflight finding codes (:class:`RuntimePreflightFindingCode`), which an
agent used to write as bare free text and which now carry one domain prefix so they
cannot collide with another domain's word; the old spellings are adopted on read
(:func:`adopt_preflight_finding_code`).  A code is a ``StrEnum``, so it
compares, hashes and serialises as its word.
"""
