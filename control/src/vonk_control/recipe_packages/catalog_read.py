"""What the Controller takes from the contract's catalog reader.

``vonk_forge_contracts.catalog_reader.read_catalog`` is the one reader of the
signed index: it verifies every entry's digest, skips an entry it cannot run,
drops an unreadable optional block and keeps a word a newer minor added. This
module turns its report into the sync's own terms:

- a skipped entry is a sync problem (it pauses retraction, like any skip);
- a dropped block is a note: the entry applies without it;
- a word this Controller does not know is noted and read without
  (``known_words``): left out of a list, read as a cautious neutral value, or,
  failing both, its enclosing object is left out. A block that cannot stand
  without it is withheld (``None``) and noted. No unknown word reaches what the
  Controller stores or serves; a stored display stays until a readable one
  arrives.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel
from vonk_agent_protocol import RecipePackageCode
from vonk_forge_contracts import CatalogModelEntry, CatalogRecipeEntry
from vonk_forge_contracts.catalog_reader import (
    CatalogProblem,
    CatalogSection,
    DroppedBlock,
    SkippedEntry,
    UnrecognisedWordReport,
)

from ..catalog_revision_contract import PublishedDocument
from ..catalog_sync_contract import ManagedCatalogSyncProblem
from .contracts import RecipePackageError
from .known_words import BlockWithheld, known_block

# Entries named in one note; the count names them all.
_NAMED = 3


@dataclass(frozen=True, slots=True)
class ReaderReport:
    problems: tuple[ManagedCatalogSyncProblem, ...]
    notes: tuple[ManagedCatalogSyncProblem, ...]
    #: The blocks carrying an unknown word, by section and ``publisher/slug``.
    unknown: Mapping[tuple[CatalogSection, str], frozenset[str]]


def report(problems: Iterable[CatalogProblem]) -> ReaderReport:
    """Map the reader's problems to sync problems, notes and unknown-word blocks."""

    skipped: list[ManagedCatalogSyncProblem] = []
    dropped: dict[tuple[CatalogSection, str, str, str], list[str]] = {}
    words: list[UnrecognisedWordReport] = []
    unknown: dict[tuple[CatalogSection, str], set[str]] = {}
    for problem in problems:
        match problem:
            case SkippedEntry():
                skipped.append(
                    _problem(
                        f"index {problem.section.value} entry "
                        f"{_shown(problem.entry)} is skipped: {_shown(problem.field)} "
                        f"is {problem.reason.value}"
                    )
                )
            case DroppedBlock():
                key = (
                    problem.section,
                    problem.block.value,
                    problem.field,
                    problem.reason.value,
                )
                dropped.setdefault(key, []).append(problem.entry)
            case UnrecognisedWordReport():
                words.append(problem)
                block = problem.field.split(".", 1)[0]
                for entry in problem.entries:
                    unknown.setdefault((problem.section, entry), set()).add(block)
    notes = [
        _problem(
            f"index {section.value} {block} dropped for {_entries(entries)}: "
            f"{_shown(field)} is {reason}"
        )
        for (section, block, field, reason), entries in dropped.items()
    ] + [
        _problem(
            f"index {word.section.value} {_shown(word.field)} word "
            f"{_shown(word.word)!r} is unknown to this Controller; read without it "
            f"for {_entries(word.entries)}"
        )
        for word in words
    ]
    return ReaderReport(
        problems=tuple(skipped),
        notes=tuple(notes),
        unknown={key: frozenset(blocks) for key, blocks in unknown.items()},
    )


def known_entries[E: (CatalogModelEntry, CatalogRecipeEntry)](
    section: CatalogSection,
    entries: Iterable[E],
    unknown: Mapping[tuple[CatalogSection, str], frozenset[str]],
) -> tuple[list[E], list[ManagedCatalogSyncProblem]]:
    """The entries with each unknown-word block rebuilt from known words.

    A block that cannot stand without its unknown words is withheld (``None``)
    and noted once per block.
    """

    known: list[E] = []
    withheld: dict[str, list[str]] = {}
    for entry in entries:
        identity = entry.document.identity
        name = f"{identity.publisher}/{identity.slug}"
        update: dict[str, BaseModel | None] = {}
        for block in sorted(unknown.get((section, name), ())):
            try:
                update[block] = known_block(getattr(entry, block))
            except BlockWithheld:
                update[block] = None
                # Noted below, once per block: never a silent absence.
                withheld.setdefault(block, []).append(name)
        known.append(entry.model_copy(update=update) if update else entry)
    notes = [
        _problem(
            f"index {section.value} {block} withheld for {_entries(names)}: "
            "it cannot stand without an unknown word"
        )
        for block, names in withheld.items()
    ]
    return known, notes


def published_documents(
    index: PublishedDocument, kept: Collection[str]
) -> dict[str, PublishedDocument]:
    """The kept entries' documents exactly as published, by their digest.

    The reader verified each kept entry's digest against this same mapping, so
    the claim names these bytes. A skipped entry claiming a kept digest with
    another document is an integrity fault of the signed index.
    """

    documents: dict[str, PublishedDocument] = {}
    for section in CatalogSection:
        entries = index.get(section.value)
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict):
                continue
            digest, document = entry.get("content_sha256"), entry.get("document")
            if (
                not isinstance(digest, str)
                or digest not in kept
                or not isinstance(document, dict)
            ):
                continue
            if documents.setdefault(digest, document) != document:
                raise RecipePackageError(
                    RecipePackageCode.RESPONSE_INVALID,
                    "recipe package index names one digest for two documents",
                )
    return documents


def _problem(detail: str) -> ManagedCatalogSyncProblem:
    return ManagedCatalogSyncProblem(
        recipe_uri=None,
        code=RecipePackageCode.DOCUMENT_INCOMPATIBLE,
        detail=detail[:256],
    )


def _entries(names: Iterable[str]) -> str:
    listed = list(names)
    shown = ", ".join(_shown(name) for name in listed[:_NAMED])
    more = f" and {len(listed) - _NAMED} more" if len(listed) > _NAMED else ""
    noun = "entry" if len(listed) == 1 else "entries"
    return f"{len(listed)} {noun} ({shown}{more})"


def _shown(text: str) -> str:
    """Index text in a status line: printable, bounded."""

    return "".join(c if c.isprintable() else "?" for c in text[:96])
