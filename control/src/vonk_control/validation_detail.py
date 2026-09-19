"""One bounded description of a stored document that will not validate.

A pydantic ``ValidationError`` stringifies the offending value, so it can never
be handed to an operator or a client as-is.  Naming the failing field and rule
is what makes a validation blocker diagnosable, and every surface that reports
one has to agree on that rendering.  Keeping it here means the receipt reader
and the stored build-envelope reader cannot drift into two spellings of the
same idea.
"""

from __future__ import annotations

from pydantic import ValidationError

from .logging import redact_text

# Two issues are enough to locate a broken document; a location plus an error
# type identifies the rule, so the redactor-approved message is the only part
# that needs a byte bound of its own.
_MAX_ISSUES = 2
_MAX_MESSAGE = 80


def validation_error_detail(error: BaseException) -> str:
    """Name the failing field and rule without carrying the document.

    A stored document that will not validate was reported as one generic
    sentence, so an operator could not tell which field or rule rejected it --
    the same blind spot the agent result boundary had.  Bounded locations,
    error types and the redactor-approved message are enough to identify the
    rule; the document itself is never included.  Returns an empty string when
    ``error`` is not a pydantic ``ValidationError``, so a caller may append the
    result unconditionally.
    """

    if not isinstance(error, ValidationError):
        return ""
    rendered: list[str] = []
    for issue in error.errors()[:_MAX_ISSUES]:
        location = ".".join(str(part) for part in issue.get("loc", ())) or "<root>"
        kind = str(issue.get("type", "invalid"))
        message = redact_text(str(issue.get("msg", "")))[:_MAX_MESSAGE]
        rendered.append(
            f"{location}:{kind}:{message}" if message else f"{location}:{kind}"
        )
    return f" ({'; '.join(rendered)})" if rendered else ""
